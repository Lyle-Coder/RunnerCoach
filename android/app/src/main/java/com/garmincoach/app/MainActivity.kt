package com.garmincoach.app

import android.app.Activity
import android.app.AlertDialog
import android.app.DatePickerDialog
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
import android.provider.DocumentsContract
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
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
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
 * 为什么不要右上角 ⋮：六个动作（一图流 / 历史 / AI / 设置 / 体重 / 登录）
 * 全塞在一个弹出菜单里，既找不到也没法体现层级。现在改成
 * **顶部工具栏 + 底部 4 个 tab（今日 / 历史 / 教练 / 我的）+ 中间 WebView**：
 * 高频浏览走 tab，低频动作放进「我的」页的卡片，页面里的 data-act 通过
 * JS 桥调回原生对话框（登录、补拉历史、出图仍然用原生最可靠）。
 * 每个 tab 对应本机服务的一个同源网页，外壳、色板、卡片统一由
 * `garmin_cn/ui.py` 生成；看板与 AI 页只补一段适配 CSS，不动各自 DOM。
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
    private lateinit var secrets: Secrets

    private var port = 0
    private var activeTab = 0
    private var dayCount = 0
    private val tabRoots = mutableListOf<LinearLayout>()
    private val tabIcons = mutableListOf<ImageView>()
    private val tabLabels = mutableListOf<TextView>()

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
            setBackgroundColor(0xFFFFFFFF.toInt())
            setPadding(dp(18), 0, dp(6), 0)
        }
        val titles = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        titles.addView(TextView(this).apply {
            text = "RunnerCoach"
            textSize = 16f
            setTextColor(0xFF1A1F1B.toInt())
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        subtitle = TextView(this).apply {
            text = "今日 · 下拉同步"
            textSize = 11f
            setTextColor(0xFF78806F.toInt())
        }
        titles.addView(subtitle)
        toolbar.addView(titles, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        // 顶栏只留标题与副标题：同步走今日页下拉，动作都在页面卡片里。
        // 以前右上角挂过「⟳」和「⋮」，和底部 tab、页内入口重复了三遍。
        shell.addView(toolbar, LinearLayout.LayoutParams(MATCH_PARENT, toolH))

        // 一条极细的分割线，比阴影轻，看板滚到底时也不会糊成一片
        shell.addView(View(this).apply { setBackgroundColor(0xFFE4E8E1.toInt()) },
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
                    if (u.startsWith("http://127.0.0.1:$port/")) {
                        v.evaluateJavascript(BIND_JS, null)
                        // 页面内的链接也会切分区（如首页点「历史活动」），
                        // 底部导航的高亮要跟着 URL 走，不能只认点击。
                        syncTabByUrl(u)
                        // 一级 tab 根页面落地后清空历史栈：否则「今日→历史→今日」
                        // 会在栈里堆三层，返回键就在 tab 之间来回跳。
                        if (isTabRoot(u)) v.clearHistory()
                    }
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
            setBackgroundColor(0xFFF6F7F4.toInt())
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
        shell.addView(View(this).apply { setBackgroundColor(0xFFE4E8E1.toInt()) },
            LinearLayout.LayoutParams(MATCH_PARENT, 1))
        shell.addView(buildTabBar(), LinearLayout.LayoutParams(MATCH_PARENT, dp(62)))
        setContentView(shell)

        thread { bootServer() }
    }

    override fun onDestroy() {
        if (this::web.isInitialized) web.destroy()
        super.onDestroy()
    }

    @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
    override fun onBackPressed() {
        if (!this::web.isInitialized) {
            super.onBackPressed()
            return
        }
        // 先问页面自己：设置抽屉开着就关掉（返回 true 表示已消费）。
        // 页面级导航交给下面统一判断，不用 canGoBack()，避免历史栈来回跳。
        web.evaluateJavascript("(window.__gcBack && window.__gcBack()) ? '1' : '0'") { res ->
            if (res != null && res.contains("1")) return@evaluateJavascript
            ui { nativeBack() }
        }
    }

    /** 返回优先级：二级页 → 所属 tab → 今日 → 退出。见 docs/交互树.md 第三节。 */
    private fun nativeBack() {
        val uri = try {
            android.net.Uri.parse(web.url ?: "")
        } catch (t: Throwable) {
            null
        }
        val path = uri?.path ?: "/"
        when {
            // 看板默认回历史；从今日页进来的（from=today）回今日
            path.startsWith("/view") -> goTab(if (uri?.getQueryParameter("from") == "today") 0 else 1)
            path.startsWith("/plan") -> goTab(0)     // 训练计划属于今日
            path.startsWith("/files") -> goTab(3)    // 产物属于我的
            activeTab != 0 -> goTab(0)               // 其它 tab 先回今日
            else -> finish()                         // 已在今日根页面，退出
        }
    }

    /** 一级 tab 的根路径；onPageFinished 用它决定要不要清历史栈。 */
    private fun isTabRoot(u: String): Boolean {
        val path = try {
            android.net.Uri.parse(u).path ?: "/"
        } catch (t: Throwable) {
            return false
        }
        return path == "/" || path == "/history" || path == "/ai" || path == "/me"
    }

    /** 本机页面只有两个入口：点活动行出图、点卡片触发原生动作。
     *  动作名由 `garmin_cn/ui.py` 的 data-act 统一下发，加动作不用改 JS。 */
    private inner class Bridge {
        @JavascriptInterface
        fun activity(id: String) {
            if (id.isBlank()) return
            ui { askPrivacy(id) }
        }

        @JavascriptInterface
        fun action(name: String) {
            ui {
                when (name) {
                    "export" -> exportCard()
                    "historySync" -> showSyncRangeDialog()
                    // 数据源行直接进对应登录，不再先弹「佳明还是高驰」的二层选择器
                    "login:garmin" -> askGarminLogin()
                    "login:coros" -> askCorosLogin()
                    "login" -> showLoginPicker()
                    "mi" -> showMiPanel()
                    "planRefresh" -> refreshPlan()
                    "amap" -> showAmapDialog()
                    "clips" -> showClips()
                    else -> toast("未知操作：$name")
                }
            }
        }
    }

    // ------------------------------------------------------------ 底部导航
    private val TAB_NAMES = arrayOf("今日", "历史", "教练", "我的")
    private val TAB_URLS = arrayOf("/", "/history", "/ai", "/me")
    private val TAB_ICONS = intArrayOf(
        R.drawable.ic_tab_today, R.drawable.ic_tab_history,
        R.drawable.ic_tab_coach, R.drawable.ic_tab_me)

    private fun buildTabBar(): LinearLayout {
        val nav = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(0xFFFFFFFF.toInt())
            setPadding(dp(4), dp(6), dp(4), dp(6))
        }
        for (i in TAB_NAMES.indices) {
            val item = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                setPadding(dp(2), dp(2), dp(2), dp(2))
                isClickable = true
                isFocusable = true
                setOnClickListener { goTab(i) }
            }
            val icon = ImageView(this).apply {
                setImageResource(TAB_ICONS[i])
                adjustViewBounds = true
            }
            val label = TextView(this).apply {
                text = TAB_NAMES[i]
                textSize = 11f
                gravity = Gravity.CENTER
            }
            item.addView(icon, LinearLayout.LayoutParams(dp(22), dp(22)))
            item.addView(label, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT))
            nav.addView(item, LinearLayout.LayoutParams(0, MATCH_PARENT, 1f))
            tabRoots.add(item)
            tabIcons.add(icon)
            tabLabels.add(label)
        }
        updateTabs()
        return nav
    }

    private fun updateTabs() {
        for (i in tabRoots.indices) {
            val on = i == activeTab
            val c = if (on) 0xFF17845F.toInt() else 0xFF78806F.toInt()
            tabIcons[i].setColorFilter(c)
            tabLabels[i].setTextColor(c)
            tabLabels[i].setTypeface(
                android.graphics.Typeface.DEFAULT,
                if (on) android.graphics.Typeface.BOLD else android.graphics.Typeface.NORMAL)
            tabRoots[i].setBackgroundColor(if (on) 0xFFE5F3ED.toInt() else 0x00000000)
        }
        if (this::subtitle.isInitialized) updateSubtitle()
    }

    /** 顶栏副标题统一显示「当前分区 · 数据天数」，切 tab 和同步后都走这里。 */
    private fun updateSubtitle() {
        val d = if (dayCount > 0) "$dayCount 天数据" else "下拉同步"
        subtitle.text = "${TAB_NAMES[activeTab]} · $d"
    }

    private fun goTab(i: Int) {
        if (i !in TAB_NAMES.indices) return
        activeTab = i
        updateTabs()
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        web.loadUrl("http://127.0.0.1:$port${TAB_URLS[i]}")
    }

    /** 页面内链接也会切分区，底部高亮跟着 URL 走，不能只认 tab 点击。 */
    private fun syncTabByUrl(url: String) {
        val path = url.substringAfter(":$port", url)
        val t = when {
            path.startsWith("/ai") -> 2
            path.startsWith("/me") || path.startsWith("/files") -> 3
            path.startsWith("/history") || path.startsWith("/view") -> 1
            else -> 0
        }
        if (t != activeTab) {
            activeTab = t
            updateTabs()
        }
    }

    // ------------------------------------------------------------ 历史补拉
    /** 只有「补拉」需要联网和账号，仍走原生对话框；查询历史已经全部网页化。 */
    private fun showSyncRangeDialog() {
        val end = todayISO()
        AlertDialog.Builder(this)
            .setTitle("拉取更多历史数据（增量补齐）")
            .setMessage("已有且稳定的日子会跳过，活动列表自动翻页补齐。\n" +
                "范围越大请求越多，请保持网络畅通。")
            .setItems(arrayOf<CharSequence>(
                "最近 90 天", "最近 180 天", "最近 365 天",
                "自定义日期区间…（最多 3 年）")) { _, which ->
                if (which < 3) {
                    val n = intArrayOf(90, 180, 365)[which]
                    runHistorySync(shiftISO(end, -(n - 1)), end)
                } else {
                    pickDateRange { a, b -> runHistorySync(a, b) }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun runHistorySync(start: String, end: String) {
        val email = secrets.email()
        val pwd = secrets.password()
        if (email.isNullOrBlank() || pwd.isNullOrBlank()) {
            toast("补历史需要佳明账号，请先在「账号登录」里登录")
            showLoginPicker()
            return
        }
        busy(true)
        subtitle.text = "正在补历史 $start ~ $end…"
        thread {
            // with_body=false 不顺带拉小米秤；force=false 走增量，避免重复请求
            val r = apiCall("sync_range", start, end, email, pwd, false, false)
            ui {
                busy(false)
                if (!r.optBoolean("ok")) {
                    setStatus(apiCall("status"))
                    val err = r.optString("error")
                    if (r.optBoolean("need_login") || r.optBoolean("need_mfa")) {
                        toast("登录已失效：$err")
                        askGarminLogin()
                    } else {
                        toast("补历史失败：$err")
                    }
                    return@ui
                }
                toast("已补 ${r.optString("range")}：新拉 ${r.optInt("fetched")} 天、" +
                    "跳过 ${r.optInt("skipped")} 天缓存、活动 ${r.optInt("activities")} 条")
                thread {
                    apiCall("dashboard")
                    setStatus(apiCall("status"))
                    ui { reload() }
                }
            }
        }
    }

    // DatePicker 的月份是 0-based；统一转成 YYYY-MM-DD 再传给 Python 侧校验。
    private fun isoOf(y: Int, m: Int, d: Int): String =
        String.format(Locale.US, "%04d-%02d-%02d", y, m + 1, d)

    private fun todayISO(): String {
        val c = Calendar.getInstance()
        return isoOf(c.get(Calendar.YEAR), c.get(Calendar.MONTH),
            c.get(Calendar.DAY_OF_MONTH))
    }

    private fun shiftISO(iso: String, days: Int): String = try {
        val p = iso.split("-")
        val c = Calendar.getInstance()
        c.set(p[0].toInt(), p[1].toInt() - 1, p[2].toInt())
        c.add(Calendar.DAY_OF_MONTH, days)
        isoOf(c.get(Calendar.YEAR), c.get(Calendar.MONTH),
            c.get(Calendar.DAY_OF_MONTH))
    } catch (t: Throwable) {
        todayISO()
    }

    private fun pickDate(title: String, initial: String, done: (String) -> Unit) {
        val p = initial.split("-")
        DatePickerDialog(
            this,
            DatePickerDialog.OnDateSetListener { _, y, m, d -> done(isoOf(y, m, d)) },
            p[0].toInt(), p[1].toInt() - 1, p[2].toInt()
        ).apply { setTitle(title) }.show()
    }

    private fun pickDateRange(done: (String, String) -> Unit) {
        pickDate("选择结束日期", todayISO()) { end ->
            pickDate("选择起始日期", shiftISO(end, -89)) { start ->
                if (start > end) toast("起始日不能晚于结束日")
                else done(start, end)
            }
        }
    }

    // ------------------------------------------------------------ 启动
    private fun bootServer() {
        val r = apiCall("start_server", 0)
        if (!r.optBoolean("ok")) {
            fatal("本地服务没起来", r.optString("error"))
            return
        }
        port = r.optInt("port")
        ui { goTab(0) }

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
        ui {
            dayCount = days
            updateSubtitle()
        }
    }

    /** 刷新当前页，不再无条件跳回首页；同步完停在用户原来所在的分区。 */
    private fun reload() {
        ui { web.reload() }
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
                    setTextColor(0xFF1A1F1B.toInt())
                    setTypeface(typeface, android.graphics.Typeface.BOLD)
                })
                addView(TextView(this@MainActivity).apply {
                    text = sub
                    textSize = 11.5f
                    setTextColor(0xFF78806F.toInt())
                })
            }, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            box.addView(r)
        }

        val g = secrets.email()
        val c = secrets.corosEmail()
        row(R.drawable.ic_logo_garmin, "Garmin 佳明国区",
            if (g.isNullOrBlank()) "邮箱密码 + 邮箱验证码" else "已登录 $g · 点击可重新登录"
        ) { askGarminLogin() }
        box.addView(View(this).apply { setBackgroundColor(0xFFE4E8E1.toInt()) },
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
            setTextColor(0xFF485046.toInt())
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
    /** 地图底图 Key：只存本机，配了之后活动图才铺高德底图。 */
    private fun showAmapDialog() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(10), dp(20), 0)
        }
        box.addView(TextView(this).apply {
            text = "填高德「Web 服务」Key 后，活动图会铺一张真实地图底图。\n\n" +
                   "换取底图时会把这次跑步的轨迹中心点发给高德；不填则只画矢量轨迹。\n" +
                   "Key 只保存在本机，不会上传。"
            textSize = 13f
            setTextColor(0xFF485046.toInt())
            setLineSpacing(dp(3).toFloat(), 1f)
        })
        val input = EditText(this).apply {
            hint = "高德 Web 服务 Key"
            setSingleLine()
        }
        box.addView(input)
        AlertDialog.Builder(this)
            .setTitle("地图底图")
            .setView(box)
            .setPositiveButton("保存") { _, _ -> saveAmapKey(input.text.toString().trim()) }
            .setNeutralButton("清除") { _, _ -> saveAmapKey("") }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun saveAmapKey(k: String) {
        busy(true)
        thread {
            val r = apiCall("set_amap_key", k)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    toast(r.optString("message",
                        if (r.optBoolean("configured")) "已保存" else "已清除"))
                    reload()
                } else {
                    toast(r.optString("error", "保存失败"))
                }
            }
        }
    }

    /** 训练计划刷新：联网拉一次佳明教练计划（2 个请求），成功后重载页面。 */
    private fun refreshPlan() {
        busy(true)
        thread {
            val r = apiCall("plan", true)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    toast("训练计划已更新")
                    reload()
                } else {
                    toast(r.optString("error", "暂时读不到佳明训练计划"))
                }
            }
        }
    }

    // ------------------------------------------------------------ 素材
    /**
     * 素材管理：每条显示缩略图 + 名称 + 类型/时长 + 拍摄时间，点一条问删除。
     *
     * 为什么不复用 `setItems(文字数组)`：只有文字时根本分不清哪条是哪条——
     * 一堆 `vid-1730...` 的名字，选错了才发现删错。缩略图 + 时间才认得出来。
     */
    private fun showClips() {
        thread {
            val r = apiCall("clips_list")
            ui {
                if (!r.optBoolean("ok")) {
                    toast("读取素材失败")
                    return@ui
                }
                clipArray = r.optJSONArray("items") ?: JSONArray()
                renderClipList()
            }
        }
    }

    /** 当前列表数据；缩略图线程回来时要用，所以挂在类上而不是闭包里。 */
    private var clipArray: JSONArray = JSONArray()

    private fun renderClipList() {
        val arr = clipArray
        if (arr.length() == 0) {
            AlertDialog.Builder(this)
                .setTitle("素材")
                .setMessage("还没有素材。\n\n" +
                    "可以加图片或视频：\n" +
                    "· 图片直接插到回放对应位置；\n" +
                    "· 视频能裁出想要的几秒（只搬样本不重编码，几秒就完成）。\n\n" +
                    "插入位置取文件里的拍摄时间；没有时间信息时按数量均匀铺在整段活动里。")
                .setPositiveButton("添加") { _, _ -> pickMedia() }
                .setNegativeButton("关闭", null)
                .show()
            return
        }

        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), dp(6), dp(10), 0)
        }
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: JSONObject()
            box.addView(clipRow(o))
            if (i < arr.length() - 1) {
                box.addView(View(this).apply { setBackgroundColor(0xFFEDEFEA.toInt()) },
                    LinearLayout.LayoutParams(MATCH_PARENT, 1).apply {
                        leftMargin = dp(66); topMargin = dp(2); bottomMargin = dp(2)
                    })
            }
        }

        // 不再自己套 ScrollView：AlertDialog 的 customPanel 本来就在滚动容器里，
        // 嵌套一层反而让触摸事件在两层滚动之间抢。
        AlertDialog.Builder(this)
            .setTitle("素材（${arr.length()}）")
            .setView(box)
            .setPositiveButton("添加") { _, _ -> pickMedia() }
            .setNegativeButton("关闭", null)
            .show()
    }

    /** 一行：56dp 缩略图 + 两行文字。 */
    private fun clipRow(o: JSONObject): View {
        val path = o.optString("path", "")
        val name = o.optString("name", "素材")
        val kind = o.optString("kind", "video")
        val exists = o.optBoolean("exists", true)
        val dur = o.optDouble("dur", 0.0)

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(2), dp(8), dp(2), dp(8))
            isClickable = true
            // 给点击一点反馈，否则按下去像是没反应
            setBackgroundResource(android.R.drawable.list_selector_background)
        }

        val thumb = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            // 圆角靠 outline 裁：给个圆角 drawable 当背景，再 clipToOutline
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(8).toFloat()
                setColor(0xFFE4E8E1.toInt())
            }
            clipToOutline = true
        }
        row.addView(thumb, LinearLayout.LayoutParams(dp(56), dp(56)))

        val texts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), 0, 0, 0)
        }
        val title = TextView(this).apply {
            text = name
            textSize = 14f
            setTextColor(0xFF1A1F1B.toInt())
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.MIDDLE
        }
        val subTxt = clipSubtitle(o, kind, dur, exists)
        val noTime = o.optLong("t_utc", 0L) <= 0L
        val sub = TextView(this).apply {
            text = subTxt
            textSize = 12f
            setTextColor(when {
                !exists -> 0xFFB3261E.toInt()
                noTime -> 0xFFB26A00.toInt()      // 橙：不是坏了，但位置会不准
                else -> 0xFF78806F.toInt()
            })
            setPadding(0, dp(3), 0, 0)
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        texts.addView(title)
        texts.addView(sub)
        row.addView(texts, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))

        // 右箭头暗示「点进去有动作」（这里是删除确认）
        val chev = TextView(this).apply {
            text = "›"
            textSize = 20f
            setTextColor(0xFFB6BDB2.toInt())
            setPadding(dp(6), 0, dp(2), 0)
        }
        row.addView(chev)

        // 点一条不直接删：先给动作菜单，否则手滑就没了。
        // 没有拍摄时间的那条额外给「重新读取」和「为什么」，别让人对着
        // 一行「加入 xx」猜到底有没有问题。
        row.setOnClickListener {
            val noTime = o.optLong("t_utc", 0L) <= 0L
            val actions: Array<CharSequence> = if (noTime)
                arrayOf("预览", "重新读取时间", "为什么没有拍摄时间？", "删除")
            else
                arrayOf("预览", "删除")
            AlertDialog.Builder(this)
                .setTitle(name)
                .setItems(actions) { _, which ->
                    when {
                        which == 0 -> previewClip(o)
                        noTime && which == 1 -> {
                            toast("正在重新读取…")
                            thread {
                                val r = apiCall("clips_reprobe", o.optString("id"))
                                val got = r.optLong("t_utc", 0L)
                                ui {
                                    toast(if (got > 0) "已补到拍摄时间"
                                    else r.optString("error", "还是读不到拍摄时间"))
                                    showClips()
                                }
                            }
                        }
                        noTime && which == 2 -> explainMissingTime(o)
                        else -> confirmDeleteClip(o.optString("id"), name)
                    }
                }
                .setNegativeButton("取消", null)
                .show()
        }

        if (exists) loadClipThumb(path, kind, dp(56)) { bmp ->
            if (bmp != null) {
                thumb.setImageBitmap(bmp)
                thumb.scaleType = ImageView.ScaleType.CENTER_CROP
            }
        }
        return row
    }

    /** 副标题：类型 + 时长 + 时间。没拍摄时间要写明，不能只丢一句「加入」。 */
    private fun clipSubtitle(o: JSONObject, kind: String, dur: Double, exists: Boolean): String {
        if (!exists) return "文件已丢失，建议删除"
        val kindTxt = if (kind == "image") "图片" else "视频"
        val durTxt = if (kind == "image") "" else " · ${"%.1f".format(dur)} 秒"
        val tMs = o.optLong("t_utc", 0L)
        if (tMs > 0) {
            val whenTxt = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(tMs))
            return "$kindTxt$durTxt · 拍摄 $whenTxt"
        }
        return "$kindTxt$durTxt · ⚠ 无拍摄时间（加入 ${o.optString("added_at", "—")}）"
    }

    /** 没有拍摄时间不是「显示问题」：回放会退化成按数量均分，位置不准。说清怎么办。 */
    private fun explainMissingTime(o: JSONObject) {
        val why = o.optString("t_why", "").ifBlank { "文件里读不到拍摄时间" }
        AlertDialog.Builder(this)
            .setTitle("为什么没有拍摄时间")
            .setMessage(
                "$why\n\n" +
                    "影响：回放里这段只能按数量均匀铺开，位置不准。\n\n" +
                    "常见原因是视频被剪辑、转发或重封装后，拍摄时间" +
                    "（mvhd.creation_time）被清掉了。很多手机把拍摄时间只写在" +
                    "相册数据库或视频日期标签里，mp4 文件本身并没有。\n" +
                    "这一版在选片那一刻就把相册时间、视频日期标签、原始文件名" +
                    "和剪辑前源文件一起取出来，登记时会写进成品，所以：\n" +
                    "· 新加的视频会自动显示「拍摄」；\n" +
                    "· 老素材的时间在当初剪辑时就丢了，点「重新读取时间」再试一次，\n" +
                    "  仍读不到就删掉这条，重新从相册添加。")
            .setPositiveButton("知道了", null)
            .show()
    }

    /** 大图预览：图片看原图，视频看第一帧（要看动态得去生成回放）。 */
    private fun previewClip(o: JSONObject) {
        val path = o.optString("path", "")
        val kind = o.optString("kind", "video")
        val name = o.optString("name", "素材")
        val iv = ImageView(this).apply {
            scaleType = ImageView.ScaleType.FIT_CENTER
            setBackgroundColor(0xFF12161B.toInt())
            minimumHeight = dp(160)
        }
        val dlg = AlertDialog.Builder(this)
            .setTitle(name)
            .setView(iv)
            .setNegativeButton("关闭", null)
            .create()
        dlg.show()
        dlg.window?.setLayout(
            (resources.displayMetrics.widthPixels * 0.92f).toInt(), WRAP_CONTENT)
        loadClipThumb(path, kind, resources.displayMetrics.widthPixels) { bmp ->
            if (bmp != null) iv.setImageBitmap(bmp)
        }
    }

    /** 取缩略图：图片降采样解码，视频抓第一帧。绝不在主线程做。 */
    private fun loadClipThumb(path: String, kind: String, maxPx: Int,
                              done: (Bitmap?) -> Unit) {
        thread {
            var bmp: Bitmap? = null
            try {
                val f = File(path)
                if (f.isFile) {
                    bmp = if (kind == "image") {
                        val b = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                        BitmapFactory.decodeFile(path, b)
                        var s = 1
                        while (b.outWidth / s > maxPx * 2 || b.outHeight / s > maxPx * 2) s *= 2
                        BitmapFactory.decodeFile(path, BitmapFactory.Options().apply {
                            inSampleSize = s
                        })
                    } else {
                        val mmr = android.media.MediaMetadataRetriever()
                        try {
                            mmr.setDataSource(path)
                            mmr.getScaledFrameAtTime(
                                0, android.media.MediaMetadataRetriever.OPTION_CLOSEST_SYNC,
                                maxPx * 2, maxPx * 2)
                        } finally {
                            try { mmr.release() } catch (_: Throwable) {}
                        }
                    }
                }
            } catch (t: Throwable) {
                Log.w(TAG, "clip thumb", t)
            }
            ui { done(bmp) }
        }
    }

    /** 相册里图片和视频都能选：图片直接入库，视频走裁剪。 */
    private fun pickMedia() {
        try {
            startActivityForResult(
                Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                    putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("video/*", "image/*"))
                },
                REQ_VIDEO,
            )
        } catch (t: Throwable) {
            toast("打不开相册：${t.message}")
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_VIDEO && resultCode == android.app.Activity.RESULT_OK) {
            val uri = data?.data ?: return
            val mime = try {
                contentResolver.getType(uri) ?: ""
            } catch (t: Throwable) {
                ""
            }
            if (mime.startsWith("image/")) addImage(uri, mime) else trimDialog(uri)
        }
    }

    /**
     * 图片素材：拷进 data/media 登记即可，不需要裁剪。
     *
     * 常见格式**原样**拷，保留 EXIF 的拍摄时间与坐标——回放靠它把素材放到正确位置。
     * HEIC 之类浏览器解不了的，解码重编成 JPEG（会丢 EXIF，位置由后端均匀铺开兜底）。
     */
    private fun addImage(src: Uri, mime: String) {
        busy(true)
        subtitle.text = "加入素材…"
        thread {
            // 扩展名跟着内容走：本机服务只放行 png/jpg，把 PNG 存成 .jpg 会让
            // Content-Type 和实际字节对不上；webp / heic 一律重编成 JPEG。
            val copyAsIs = mime == "image/jpeg" || mime == "image/jpg" || mime == "image/png"
            val ext = if (mime == "image/png") ".png" else ".jpg"
            val dst = File(File(filesDir, "data/media"),
                "img-" + System.currentTimeMillis() + ext)
            dst.parentFile?.mkdirs()
            var ok = false
            if (copyAsIs) {
                try {
                    contentResolver.openInputStream(src)?.use { i ->
                        dst.outputStream().use { o -> i.copyTo(o) }
                    }
                    ok = dst.length() > 0
                } catch (t: Throwable) {
                    Log.w(TAG, "copy image", t)
                }
            }
            if (!ok) {
                try {
                    val bmp = decodeDown(src, 1600)
                    if (bmp != null) {
                        dst.outputStream().use { o ->
                            bmp.compress(Bitmap.CompressFormat.JPEG, 88, o)
                        }
                        bmp.recycle()
                        ok = dst.length() > 0
                    }
                } catch (t: Throwable) {
                    Log.w(TAG, "encode image", t)
                }
            }
            if (!ok) {
                dst.delete()
                ui { busy(false); updateSubtitle(); toast("这张图片读不出来") }
                return@thread
            }
            val opt = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(dst.absolutePath, opt)
            val add = apiCall("clips_add", dst.absolutePath, dst.nameWithoutExtension,
                0.0, opt.outWidth, opt.outHeight, "image")
            ui {
                busy(false)
                updateSubtitle()
                if (add.optBoolean("ok")) {
                    toast("已加入素材（图片）")
                    showClips()          // 回到列表，能立刻看到刚加的那条
                } else {
                    toast(add.optString("error", "登记失败"))
                }
            }
        }
    }

    /** 按最长边降采样解码，避免 4000 万像素的图把内存打爆。 */
    private fun decodeDown(uri: Uri, maxSide: Int): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        try {
            contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        } catch (_: Throwable) {
        }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var s = 1
        while (maxOf(bounds.outWidth, bounds.outHeight) / s > maxSide) s *= 2
        val opt = BitmapFactory.Options().apply { inSampleSize = s }
        return try {
            contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, opt) }
        } catch (t: Throwable) {
            Log.w(TAG, "decode image", t)
            null
        }
    }

    /** 视频自己写的拍摄日期：`20261107T185530.000Z` / `2026-11-07 18:55:30+0800`。 */
    private fun parseMediaDate(s: String?): Long {
        if (s.isNullOrBlank()) return 0L
        val m = Regex("(\\d{4})[-/]?(\\d{2})[-/]?(\\d{2})[T ]?(\\d{2}):?(\\d{2}):?(\\d{2})")
            .find(s) ?: return 0L
        val (y, mo, d, h, mi, se) = m.destructured
        val off = Regex("([+-])(\\d{2}):?(\\d{2})$").find(s.trim())
        val tz = when {
            s.trim().endsWith("Z", true) -> "UTC"
            off != null -> "GMT${off.groupValues[1]}${off.groupValues[2]}:${off.groupValues[3]}"
            else -> TimeZone.getDefault().id
        }
        return try {
            Calendar.getInstance(TimeZone.getTimeZone(tz)).apply {
                set(Calendar.YEAR, y.toInt())
                set(Calendar.MONTH, mo.toInt() - 1)
                set(Calendar.DAY_OF_MONTH, d.toInt())
                set(Calendar.HOUR_OF_DAY, h.toInt())
                set(Calendar.MINUTE, mi.toInt())
                set(Calendar.SECOND, se.toInt())
                set(Calendar.MILLISECOND, 0)
            }.timeInMillis
        } catch (t: Throwable) {
            0L
        }
    }

    /** 先把选中的视频拷进缓存（extractor 需要文件路径），再弹裁剪。 */
    private fun trimDialog(src: Uri) {
        busy(true)
        thread {
            // 原始文件名要留住：`clip-<时间戳>` 这种名字既看不出内容，
            // 也丢了相机写在名字里的拍摄时间（VID_20261103_180036）。
            // 同时把相册的 DATE_TAKEN / 文件修改时间取出来：真机上很多视频的
            // 拍摄时间只存在于 MediaStore，mp4 里的 mvhd 是 0，光探文件永远读不到。
            var storeTakenMs = 0L
            val displayName = try {
                contentResolver.query(src, null, null, null, null)?.use { c ->
                    if (!c.moveToFirst()) return@use null
                    fun col(name: String): Long = try {
                        val i = c.getColumnIndex(name)
                        if (i >= 0 && !c.isNull(i)) c.getLong(i) else 0L
                    } catch (t: Throwable) {
                        0L
                    }
                    val dt = col("datetaken")
                    val lm = col(DocumentsContract.Document.COLUMN_LAST_MODIFIED)
                    storeTakenMs = if (dt > 0) dt else lm
                    val ni = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                    if (ni >= 0) c.getString(ni) else null
                }
            } catch (t: Throwable) {
                null
            } ?: "video.mp4"
            val tmp = File(cacheDir, "clip-src-${System.currentTimeMillis()}.mp4")
            try {
                contentResolver.openInputStream(src)?.use { i ->
                    tmp.outputStream().use { o -> i.copyTo(o) }
                }
            } catch (t: Throwable) {
                ui { busy(false); toast("读取视频失败：${t.message}") }
                return@thread
            }
            var durMs = 0L
            var metaDateMs = 0L
            val mmr = android.media.MediaMetadataRetriever()
            try {
                mmr.setDataSource(tmp.absolutePath)
                durMs = mmr.extractMetadata(
                    android.media.MediaMetadataRetriever.METADATA_KEY_DURATION)
                    ?.toLongOrNull() ?: 0L
                metaDateMs = parseMediaDate(mmr.extractMetadata(
                    android.media.MediaMetadataRetriever.METADATA_KEY_DATE))
            } catch (t: Throwable) {
                Log.w(TAG, "read duration", t)
            } finally {
                try { mmr.release() } catch (_: Throwable) {}
            }
            // 视频自己的日期标签 > 相册 DATE_TAKEN > 文件修改时间。
            // DATE_ADDED 故意不用：那是「下载/加入媒体库」的时间，不是拍摄时间。
            val takenMs = if (metaDateMs > 0) metaDateMs else storeTakenMs
            ui {
                busy(false)
                if (durMs < 1500) {
                    tmp.delete()
                    toast("这段视频太短（不足 1.5 秒）")
                } else {
                    showTrimUi(tmp, durMs, displayName, takenMs)
                }
            }
        }
    }

    private fun showTrimUi(src: File, durMs: Long, displayName: String, takenMs: Long) {
        // 预览 + 关键帧胶片条：光有两个滑块根本不知道第几秒拍的是什么，
        // 也没法知道落刀会不会被吸附到别处。见 ClipTrimDialog。
        ClipTrimDialog.show(this, src, durMs,
            onPick = { s, e -> saveClip(src, s, e, displayName, takenMs) },
            onCancel = { src.delete() })
    }

    private fun saveClip(src: File, startMs: Long, endMs: Long,
                         displayName: String, takenMs: Long) {
        busy(true)
        subtitle.text = "剪辑中…"
        thread {
            val dst = File(File(filesDir, "data/media"),
                "clip-" + System.currentTimeMillis() + ".mp4")
            val r = ClipTrimmer.trim(src, dst, startMs, endMs)
            if (!r.ok) {
                src.delete()
                ui { busy(false); updateSubtitle(); toast("剪辑失败：${r.message}") }
                return@thread
            }
            // 关键：把**源文件**和**相册拍摄时间**都传过去。MediaMuxer 重封装会丢
            // creation_time；不少手机把拍摄时间只写在 MediaStore / 解码器日期标签里，
            // 裁完的成品和拷进 cache 的源片都读不出来，只能在选片这一刻取走。
            // name 是相册原始文件名，最后还能从 VID_20261103_180036 兜一层。
            val add = apiCall("clips_add", dst.absolutePath, displayName,
                r.durMs / 1000.0, r.width, r.height, "video", src.absolutePath, takenMs)
            src.delete()
            ui {
                busy(false)
                updateSubtitle()
                if (add.optBoolean("ok")) {
                    // 实际落刀点可能比选定的早（吸附到关键帧），说出来免得以为剪错了
                    val drift = startMs - r.startMs
                    val extra = if (drift > 200) "，实际从 ${r.startMs / 1000.0} 秒开始" else ""
                    val noTime = if (add.optLong("t_utc", 0L) <= 0L) "，⚠ 没读到拍摄时间" else ""
                    toast("已加入素材（${r.durMs / 1000} 秒$extra）$noTime")
                    showClips()          // 回到列表，能立刻看到刚剪的那条
                } else {
                    toast(add.optString("error", "登记失败"))
                }
            }
        }
    }

    private fun confirmDeleteClip(id: String, name: String) {
        AlertDialog.Builder(this)
            .setTitle("删除素材")
            .setMessage("删除「$name」？文件也会一起删掉。")
            .setPositiveButton("删除") { _, _ ->
                thread {
                    apiCall("clips_remove", id)
                    ui { toast("已删除"); showClips() }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

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
                    "选好档位后才会生成图片并分享；原图会先存进相册。\n" +
                    "配过地图 Key 时，「原图」和「裁剪」会铺地图底图（裁剪档只用中间段取景），" +
                    "「隐藏轨迹」不显示地图。"
            )
            .setSingleChoiceItems(PRIVACY_LABELS, checked) { _, which -> checked = which }
            .setNeutralButton("回放视频") { _, _ ->
                val mode = PRIVACY_MODES[checked]
                prefs.edit().putString("share_privacy", mode).apply()
                exportMotion(id, mode, 30)
            }
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
            // 第 5 个参数是分享脱敏档位；第 6/7 个是高驰凭据——高驰活动的明细
            // 走高驰接口抓，绝不能弹佳明登录框（旧内核只有 4 个形参，
            // 所以这个 APK 必须与新 Python 内核一起打包：sync_python.bat）。
            val r = apiCall("act_card_html", id, null, secrets.email(),
                            secrets.password(), privacy,
                            secrets.corosEmail() ?: "", secrets.corosPassword() ?: "")
            ui {
                if (!r.optBoolean("ok")) {
                    busy(false)
                    if (r.optBoolean("need_login")) {
                        toast("这条活动的明细需要先联网登录抓一次")
                        if (r.optString("source") == "coros") askCorosLogin() else askGarminLogin()
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

    /** 轨迹回放视频：页面逐帧推帧 → 原生 MediaCodec 编码，不依赖 ffmpeg。 */
    private fun exportMotion(id: String, privacy: String, duration: Int = 30) {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        busy(true)
        subtitle.text = "生成回放 0%"
        thread {
            val r = apiCall("motion_html", id, 24, duration, privacy)
            if (!r.optBoolean("ok")) {
                ui {
                    busy(false)
                    updateSubtitle()
                    toast(r.optString("error", "生成回放失败"))
                }
                return@thread
            }
            ui {
                startMotionView(
                    r.optString("run"), r.optInt("frames"), r.optInt("fps", 24),
                    r.optString("html"), r.optString("name", "跑步回放"),
                )
            }
        }
    }

    /**
     * 离屏 WebView 加载动画页：页面自己逐帧 JPEG POST 到 /__frame。
     *
     * v42 之前是「等 720 帧全部写盘 → 才开始编码」两段串行，30 秒的片子要等
     * 两遍耗时。现在 WebView 一起就起编码线程，按帧号边收边编；帧齐立刻拆掉
     * WebView 释放内存（编码只读盘上的 JPEG，不再依赖它）。
     */
    private fun startMotionView(run: String, frames: Int, fps: Int,
                                htmlPath: String, title: String) {
        val name = htmlPath.substringAfterLast('/')
        val url = "http://127.0.0.1:$port/$name?export=1&total=$frames&fps=$fps&run=$run"
        val view = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.textZoom = 100
            // 离屏也必须挂进窗口才会真正渲染；软件层避免取不到内容
            setLayerType(View.LAYER_TYPE_SOFTWARE, null)
            translationY = 8000f
        }
        pull.addView(view, FrameLayout.LayoutParams(1080, 1920))

        val cancel = AtomicBoolean(false)
        val encoded = AtomicBoolean(false)
        val detached = AtomicBoolean(false)
        val rendered = AtomicBoolean(false)
        val renderDone = AtomicInteger(0)
        val encodeDone = AtomicInteger(0)
        val startedAt = System.currentTimeMillis()
        // 全流水线共享的「最后一次有进展」时刻：渲染侧帧数增长、编码侧每拿到
        // 一帧或编完一帧都会刷新它。慢机器只要还在推进就一直续期，不会超时。
        val lastProgressAt = AtomicLong(System.currentTimeMillis())
        val pctLock = Any()
        var lastPct = -1
        // 并行流水线的真实完成度 = 渲染与编码里**较慢**的那条。
        // v42 用 max(渲染 0~70, 编码 70~100)：编码编完第 0 帧就报 70，
        // 进度条瞬间假跳到 70%，之后渲染侧所有上报都被单调保护吞掉，
        // 只剩每 24~29 帧才涨 1% 的编码刻度。这里改成两条线程各报帧数，
        // 统一按 min(r, e) 换算，绝不虚高。
        fun report(force: Int? = null) {
            val v = force ?: (minOf(renderDone.get(), encodeDone.get()) * 100L /
                max(1, frames)).toInt().coerceIn(0, 100)
            val show = synchronized(pctLock) {
                if (v <= lastPct) -1 else {
                    lastPct = v
                    v
                }
            }
            if (show >= 0) ui { subtitle.text = "生成回放 $show%" }
        }
        fun detach() {
            if (!detached.compareAndSet(false, true)) return
            ui {
                try {
                    pull.removeView(view)
                } catch (t: Throwable) {
                    Log.w(TAG, "remove motion view", t)
                }
                view.destroy()
            }
        }
        report()
        view.loadUrl(url)

        // 只管显示、诊断与兜底取消，不再决定什么时候开始编码
        thread {
            var lastFrames = -1
            while (!encoded.get() && !cancel.get()) {
                Thread.sleep(700)
                val st = apiCall("motion_status", run)
                val n = st.optInt("frames")
                if (n > lastFrames) {
                    lastFrames = n
                    lastProgressAt.set(System.currentTimeMillis())
                }
                renderDone.set(n)
                report()
                if (n >= frames && n > 0) {
                    if (rendered.compareAndSet(false, true)) {
                        Log.i(TAG, "motion render done: " +
                            "${(System.currentTimeMillis() - startedAt) / 1000}s, " +
                            "$n frames")
                    }
                    detach()
                }
                // 慢但持续推进时 lastProgressAt 一直被刷新，这里永远不触发。
                // 只有连续 grace 分钟一帧都没动，才判定页面/编码器真的卡死。
                // 诊断也放到卡死时才判：旧版「30 秒还没 0 帧」会把加载慢的
                // WebView 误杀（而且 diag 是空对象，那个分支其实从来没命中）。
                val grace = if (lastFrames > 0) MOTION_STALL_MS
                            else MOTION_FIRST_FRAME_MS
                if (System.currentTimeMillis() - lastProgressAt.get() <= grace) continue
                val diag = st.optJSONObject("diag")
                val hasDiag = diag != null && diag.length() > 0
                val baseOk = diag?.optBoolean("base", true) ?: true
                val msg = when {
                    lastFrames <= 0 && !baseOk ->
                        "底图没能加载：先到「我的 → 地图底图」配置高德 Key，再试一次"
                    lastFrames <= 0 && !hasDiag ->
                        "播放页没有开始渲染（WebView 未就绪），请重试"
                    else -> "回放生成已停止：连续 ${grace / 60_000} 分钟没有新帧" +
                        "（已收 ${max(0, lastFrames)}/$frames 帧），请重试"
                }
                Log.w(TAG, "motion stalled: $msg, render=$n/$frames, " +
                    "encode=${encodeDone.get()}/$frames")
                cancel.set(true)
                apiCall("motion_cleanup", run)
                detach()
                ui { busy(false); updateSubtitle(); toast(msg) }
                break
            }
        }

        // 编码线程：与 WebView 渲染并行
        thread {
            val ok = encodeMotion(run, fps, title, frames, cancel, startedAt,
                lastProgressAt,
                { done ->
                    encodeDone.set(done)
                    if (done > 0) lastProgressAt.set(System.currentTimeMillis())
                    report()
                }, { detach() })
            encoded.set(true)
            if (ok) report(100)   // 只有成功才强制到 100%，超时后别显示满格
            detach()
            // 统一收尾：不管成功、失败、诊断取消还是超时，进度圈都得关掉
            ui { busy(false); updateSubtitle() }
        }
    }

    /** 等第 i 帧落盘。服务端用 .tmp + os.replace 原子改名，文件出现即完整。
     *  超时看的是**整条流水线的心跳**，不是这一帧等了多久：渲染慢时每帧可能
     *  要好几秒甚至几十秒，旧版固定 120 秒死线会在正常推进中把编码器掐断。 */
    private fun awaitFrame(dir: File, i: Int, cancel: AtomicBoolean,
                           lastProgressAt: AtomicLong): File? {
        val f = File(dir, String.format(Locale.US, "f_%05d.jpg", i))
        while (!cancel.get()) {
            if (f.isFile && f.length() > 1024) {
                lastProgressAt.set(System.currentTimeMillis())
                return f
            }
            if (System.currentTimeMillis() - lastProgressAt.get() >
                MOTION_STALL_MS) return null
            Thread.sleep(12)
        }
        return null
    }

    private fun encodeMotion(run: String, fps: Int, title: String, total: Int,
                             cancel: AtomicBoolean, startedAt: Long,
                             lastProgressAt: AtomicLong,
                             report: (Int) -> Unit, detach: () -> Unit): Boolean {
        val st = apiCall("motion_status", run)
        val dir = File(st.optString("dir"))
        // 页面是先 POST 时间轴、再推第 0 帧。等到时间轴和首帧都在，
        // 音频清单就一定是齐的，不会出现「编完了才发现没混音」。
        // 不再自带 60 秒死线：首帧慢交给监督线程按「零进展」统一判定，
        // 也由它给出具体原因，这里只管等 cancel。
        var ready = false
        while (!cancel.get()) {
            if (File(dir, "_timeline.json").isFile && File(dir, "f_00000.jpg").isFile) {
                ready = true
                break
            }
            // 监督线程万一挂了，这里也不能无限空转。门槛必须比它的
            // FIRST_FRAME + STALL 更宽，保证正常情况下先由监督线程给出原因。
            if (System.currentTimeMillis() - lastProgressAt.get() >
                MOTION_FIRST_FRAME_MS + MOTION_STALL_MS) break
            Thread.sleep(200)
        }
        if (!ready || cancel.get()) {
            // cancel=true 说明监督线程已经弹过具体诊断，别重复弹
            if (!cancel.get()) ui { toast("没有收到帧，回放生成失败，请重试") }
            return false
        }
        val out = File(File(filesDir, "output"),
            "motion-" + SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date()) + ".mp4")
        // 时间轴落在帧目录里，cleanup 会连它一起删，所以先取再编码。
        // 取不到（没视频素材 / 老页面没回传）就是空列表，编码器退回无声。
        val ar = apiCall("motion_audio", run)
        val segments = ArrayList<MotionAudio.Segment>()
        val rows = ar.optJSONArray("items")
        if (rows != null) {
            for (i in 0 until rows.length()) {
                val o = rows.optJSONObject(i) ?: continue
                val f = o.optString("file", "")
                if (f.isNotEmpty()) {
                    segments.add(MotionAudio.Segment(
                        f, o.optDouble("at", 0.0), o.optDouble("dur", 0.0)))
                }
            }
        }
        val r = MotionEncoder.encode(
            dir, out, 1080, 1920, fps, segments,
            frameCount = total,
            frameAt = MotionEncoder.FrameAt { i ->
                awaitFrame(dir, i, cancel, lastProgressAt)
            },
            cancelled = { cancel.get() },
            onProgress = report)
        apiCall("motion_cleanup", run)
        if (cancel.get()) return false   // 监督线程已经弹过具体原因
        detach()
        val seconds = (System.currentTimeMillis() - startedAt) / 1000
        Log.i(TAG, "motion export done: ${seconds}s, ${r.via} ${r.width}x${r.height}, " +
            "ok=${r.ok}, audio=${r.audioSegments}, frames=$total, msg=${r.message}")
        ui {
            busy(false)
            updateSubtitle()
            if (!r.ok) {
                toast("编码失败：${r.message}")
                return@ui
            }
            val uri = saveVideoToGallery(out)
            if (uri == null) {
                toast("回放已生成，但写不进相册：${out.name}")
                return@ui
            }
            val tail = if (r.audioSegments > 0)
                "（含 ${r.audioSegments} 段素材原声）" else ""
            toast("回放已存相册/GarminCoach · ${seconds}s · ${r.via} 编码$tail")
            shareVideo(uri, "分享回放：$title")
        }
        return r.ok && !cancel.get()     // 只有真的成了才把进度推到 100%
    }

    private fun saveVideoToGallery(f: File): Uri? = try {
        val values = ContentValues().apply {
            put(MediaStore.Video.Media.DISPLAY_NAME, f.name)
            put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
            put(MediaStore.Video.Media.RELATIVE_PATH,
                Environment.DIRECTORY_MOVIES + "/GarminCoach")
        }
        val uri = contentResolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
        if (uri == null) null else {
            contentResolver.openOutputStream(uri)?.use { out ->
                f.inputStream().use { it.copyTo(out) }
            }
            uri
        }
    } catch (t: Throwable) {
        Log.e(TAG, "save video", t)
        null
    }

    private fun shareVideo(uri: Uri, title: String) {
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "video/mp4"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(i, title))
    }

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

        /**
         * 回放导出的超时语义（v44）：**慢不是失败，卡死才是**。
         *
         * 旧版按「从点击起 12 分钟」一刀切：真机一帧要几秒时，进度还在走，
         * 到点却直接 cancel 并弹「回放生成超时，请重试」；用户重试又是同样的
         * 12 分钟，永远出不了片。现在只认「连续这么久没有任何新帧」：只要
         * 渲染帧数或编码帧数还在增长就一直续期，没有总时长上限。
         * 首帧前单独放宽，因为 WebView 要加载底图、照片和视频。
         */
        const val MOTION_STALL_MS = 5 * 60 * 1000L
        const val MOTION_FIRST_FRAME_MS = 3 * 60 * 1000L

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

        /** 相册选素材的请求号。 */
        const val REQ_VIDEO = 1001

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
              s.textContent = 'tr[data-aid]{cursor:pointer} tr[data-aid]:active{background:rgba(23,132,95,.08)}';
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
