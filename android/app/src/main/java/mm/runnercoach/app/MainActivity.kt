package mm.runnercoach.app

import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.app.Activity
import android.app.AlertDialog
import android.app.Dialog
import android.content.ContentValues
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.SystemClock
import android.provider.MediaStore
import android.provider.DocumentsContract
import android.system.Os
import android.util.Base64
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.ViewGroup
import android.view.Window
import android.view.View
import android.view.animation.OvershootInterpolator
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
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

    // 横屏全屏时要收起来的「外壳」：顶栏 + 上下两条 1px 分割线 + 底部 tab 栏。
    // manifest 声明了 configChanges="orientation|screenSize"，转屏时 Activity
    // 不重建，只能靠 onConfigurationChanged 手动切；否则横屏下 WebView 仍被
    // 夹在 52dp + 62dp 两栏中间，实际高度只剩 ~250dp，紧凑大屏会被压扁。
    // 2dp 进度条不收：同步时还得看得见进度，占高可以忽略。
    private val chromeBars = mutableListOf<View>()

    // 开屏：windowBackground 先给静态品牌底，这一层负责渐进辉光的呼吸动画。
    // 首屏 onPageFinished 后淡出；9 秒兜底，服务起不来也不会把界面糊住。
    private var splash: FrameLayout? = null
    private var splashStack: View? = null
    private var splashGlow: View? = null
    private var splashAnim: ObjectAnimator? = null
    private var splashAt = 0L
    private var splashGone = false

    /** 系统「用 RunnerCoach 打开 GPX」传进来的文件；等服务起来后再导入。 */
    private var pendingGpx: Uri? = null

    /** 附件要落到哪一场：按钮上带着 slug，不回落到「下一场」 */
    private var pendingAttachSlug: String? = null
    // 页面里 <input type=file> 的回调，选完照片要把 Uri 交回去，否则 WebView 一直等
    private var pendingFileCallback: android.webkit.ValueCallback<Array<Uri>>? = null

    private val api: PyObject by lazy { Python.getInstance().getModule("garmin_cn.appapi") }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        secrets = Secrets(this)
        // 从文件管理器 / 微信「其他应用打开」进来时，intent 已经带着 GPX 了；
        // 本地服务还没起，先记下来，bootServer 完成后再导入。
        pendingGpx = gpxFrom(intent)

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

        // 内容根必须不透明：windowBackground 是带图标的开屏底，Activity 画出来
        // 之后若不盖住，1px 分割线和留白处会漏出那枚图标。
        val shell = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(0xFF070B12.toInt())
        }
        val toolH = dp(52)

        val toolbar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(0xFF070B12.toInt())
            setPadding(dp(18), 0, dp(6), 0)
        }
        // 顶栏左侧的品牌角标：与启动器 / 开屏 / 一图流同一个图形。
        toolbar.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_brand_mark)
            background = resources.getDrawable(R.drawable.brand_badge_bg, theme)
            scaleType = ImageView.ScaleType.FIT_CENTER
            setPadding(dp(2), dp(2), dp(2), dp(2))
        }, LinearLayout.LayoutParams(dp(30), dp(30)).apply { rightMargin = dp(10) })
        val titles = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        titles.addView(TextView(this).apply {
            text = "RunnerCoach"
            textSize = 16f
            setTextColor(0xFFE8F0FB.toInt())
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        subtitle = TextView(this).apply {
            text = "今日 · 下拉同步"
            textSize = 11f
            setTextColor(0xFF7C8FA8.toInt())
        }
        titles.addView(subtitle)
        toolbar.addView(titles, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        // 顶栏只留标题与副标题：同步走今日页下拉，动作都在页面卡片里。
        // 以前右上角挂过「⟳」和「⋮」，和底部 tab、页内入口重复了三遍。
        chromeBars.add(toolbar)
        shell.addView(toolbar, LinearLayout.LayoutParams(MATCH_PARENT, toolH))

        // 一条极细的分割线，比阴影轻，看板滚到底时也不会糊成一片
        val topDivider = View(this).apply { setBackgroundColor(0xFF16202F.toInt()) }
        chromeBars.add(topDivider)
        shell.addView(topDivider, LinearLayout.LayoutParams(MATCH_PARENT, 1))

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
            // 页面里的 <input type=file> 需要这个回调，否则点了毫无反应。
            // 姿态评估要拍照上传，走的是标准 file input（浏览器和 WebView 同一套代码）。
            webChromeClient = object : android.webkit.WebChromeClient() {
                override fun onShowFileChooser(
                    v: WebView?,
                    cb: android.webkit.ValueCallback<Array<Uri>>?,
                    p: FileChooserParams?
                ): Boolean {
                    pendingFileCallback?.onReceiveValue(null)
                    pendingFileCallback = cb
                    val i = Intent(Intent.ACTION_GET_CONTENT).apply {
                        addCategory(Intent.CATEGORY_OPENABLE)
                        type = "image/*"
                    }
                    return try {
                        startActivityForResult(
                            Intent.createChooser(i, "选择照片"), REQ_FILE_CHOOSER)
                        true
                    } catch (t: Throwable) {
                        pendingFileCallback = null
                        toast("打不开相册：" + (t.message ?: t.javaClass.simpleName))
                        false
                    }
                }
            }
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
                        hideSplash()          // 首屏出来了，开屏可以退场
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
            setBackgroundColor(0xFF070B12.toInt())
        }
        pull.addView(web, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        pull.attach(web)
        // 同步不再有按钮：页面到顶后继续下拉就是「同步 + 重出看板」
        pull.onRefresh = { onSync() }

        // 顶栏下面一条 2dp 的进度线，不再用居中大圆圈盖住内容 ——
        // 出视频要跑几分钟，中间那圈转着，页面看得见却点不动，像卡死了。
        // 有真实进度就 determinate（busy(true, pct)），没有就跑马灯。
        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            isIndeterminate = true
            progressTintList = android.content.res.ColorStateList.valueOf(0xFF3B82F6.toInt())
            backgroundTintList = android.content.res.ColorStateList.valueOf(0x1A60A5FA)
        }
        shell.addView(bar, LinearLayout.LayoutParams(MATCH_PARENT, dp(2)))
        bar.visibility = View.GONE

        shell.addView(pull, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        val bottomDivider = View(this).apply { setBackgroundColor(0xFF16202F.toInt()) }
        val tabBar = buildTabBar()
        chromeBars.add(bottomDivider)
        chromeBars.add(tabBar)
        shell.addView(bottomDivider, LinearLayout.LayoutParams(MATCH_PARENT, 1))
        shell.addView(tabBar, LinearLayout.LayoutParams(MATCH_PARENT, dp(62)))

        // 根改成 FrameLayout：开屏是盖在整个界面之上的一层，不能占 LinearLayout
        // 的一行，也不能让底下的 Python 启动/首屏加载露出空白。
        val root = FrameLayout(this)
        root.addView(shell, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        val sp = buildSplash()
        splash = sp
        root.addView(sp, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        setContentView(root)
        // 冷启动就可能已经是横屏（平板、折叠屏、上锁定的设备），先按当前方向收一次。
        applyChromeForOrientation()
        splashAt = SystemClock.elapsedRealtime()
        startSplashAnim()
        sp.postDelayed({ hideSplash() }, 9000L)

        thread { bootServer() }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        val u = gpxFrom(intent) ?: return
        if (port > 0) importGpx(u) else pendingGpx = u
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applyChromeForOrientation()
    }

    /**
     * 横屏把上下两栏收起来，WebView 吃满整屏；竖屏原样恢复。
     * Activity 不重建，这里只改 visibility：WebView 会跟着 LinearLayout 的 weight
     * 重新测量，CSS 的 100dvh / orientation 断点自动重新求值，不必 reload。
     * 横屏是纯看板态：要切分区把手机转回竖屏，系统旋转按钮本身就是入口。
     */
    private fun applyChromeForOrientation() {
        val land = resources.configuration.orientation ==
            Configuration.ORIENTATION_LANDSCAPE
        for (v in chromeBars) v.visibility = if (land) View.GONE else View.VISIBLE
    }

    /** 开屏动画层：渐进底 + 呼吸辉光 + 品牌图块放大浮现 + 跑动进度线。 */
    private fun buildSplash(): FrameLayout {
        val f = FrameLayout(this).apply {
            // 只用渐变+辉光底：splash_bg 自带一枚居中图标，再叠 stack 里
            // 那枚放大的 tile 会在文字上方露出半截圆环（两个标志重叠）。
            background = resources.getDrawable(R.drawable.splash_surface, theme)
            isClickable = true              // 淡出前别把点击漏给下面的 WebView
        }
        val glow = View(this).apply {
            background = resources.getDrawable(R.drawable.splash_glow, theme)
            alpha = 0.32f
        }
        splashGlow = glow
        f.addView(glow, FrameLayout.LayoutParams(dp(360), dp(360), Gravity.CENTER))

        val stack = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            alpha = 0f
            scaleX = 0.9f
            scaleY = 0.9f
        }
        splashStack = stack
        val tile = FrameLayout(this).apply {
            background = GradientDrawable(
                GradientDrawable.Orientation.TL_BR,
                intArrayOf(0xFF1B3352.toInt(), 0xFF091320.toInt())).apply {
                cornerRadius = dp(32).toFloat()
                setStroke(1, 0x6660A5FA.toInt())
            }
        }
        tile.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_brand_mark)
            scaleType = ImageView.ScaleType.FIT_CENTER
            setPadding(dp(6), dp(6), dp(6), dp(6))
        }, FrameLayout.LayoutParams(dp(116), dp(116), Gravity.CENTER))
        stack.addView(tile, LinearLayout.LayoutParams(dp(132), dp(132)))
        stack.addView(TextView(this).apply {
            text = "RunnerCoach"
            textSize = 25f
            setTextColor(0xFFE8F0FB.toInt())
            setTypeface(typeface, android.graphics.Typeface.BOLD)
            letterSpacing = 0.09f
            gravity = Gravity.CENTER
            setShadowLayer(20f, 0f, 0f, 0x993B82F6.toInt())
        }, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { topMargin = dp(24) })
        stack.addView(TextView(this).apply {
            text = "正在准备今日数据…"
            textSize = 12.5f
            setTextColor(0xFF8FA4BF.toInt())
            letterSpacing = 0.06f
        }, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { topMargin = dp(8) })
        stack.addView(ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            isIndeterminate = true
            progressTintList = android.content.res.ColorStateList.valueOf(0xFF3B82F6.toInt())
            backgroundTintList = android.content.res.ColorStateList.valueOf(0x1A60A5FA)
        }, LinearLayout.LayoutParams(dp(186), dp(3)).apply { topMargin = dp(28) })
        f.addView(stack, FrameLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT, Gravity.CENTER))
        return f
    }

    private fun startSplashAnim() {
        splashStack?.animate()?.alpha(1f)?.scaleX(1f)?.scaleY(1f)
            ?.setDuration(640)?.setInterpolator(OvershootInterpolator(0.7f))?.start()
        val g = splashGlow ?: return
        splashAnim = ObjectAnimator.ofFloat(g, "alpha", 0.28f, 0.82f).apply {
            duration = 1900
            repeatCount = ValueAnimator.INFINITE
            repeatMode = ValueAnimator.REVERSE
            start()
        }
    }

    /** 最短展示 0.95 秒，避免首屏秒开时一闪而过；淡出后把整层摘掉。 */
    private fun hideSplash() {
        if (splashGone) return
        splashGone = true
        val s = splash ?: return
        val wait = (950L - (SystemClock.elapsedRealtime() - splashAt)).coerceAtLeast(0L)
        s.postDelayed({
            splashAnim?.cancel()
            s.animate().alpha(0f).setDuration(260).withEndAction {
                (s.parent as? ViewGroup)?.removeView(s)
                splash = null
                splashStack = null
                splashGlow = null
                splashAnim = null
            }.start()
        }, wait)
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
            path.startsWith("/view") -> goTab(
                if (uri?.getQueryParameter("from") == "today") TAB_TODAY else TAB_HISTORY)
            path.startsWith("/plan") -> goTab(TAB_TODAY)   // 训练计划属于今日
            path.startsWith("/event") -> goTab(TAB_COACH)  // 赛事页属于教练（/ai 里的「备赛」）
            path.startsWith("/posture") -> goTab(TAB_COACH) // 姿态评估也在教练下
            path.startsWith("/files") -> goTab(TAB_ME)     // 产物属于我的
            path.startsWith("/privacy") -> goTab(TAB_ME)   // 隐私说明也挂在我的下面
            path.startsWith("/sports") -> goTab(TAB_ME)    // 运动设置属于我的
            activeTab != TAB_TODAY -> goTab(TAB_TODAY)     // 其它 tab 先回今日
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
                    // 旧页面缓存里可能还有这两个按钮：都转成后台补全，
                    // 不再弹那个「选区间」的框（选了也没有任何可见效果）。
                    "historySync", "historySyncAll" -> runBackfill()
                    // 数据源行直接进对应登录，不再先弹「佳明还是高驰」的二层选择器
                    "login:garmin" -> openAccount("garmin")
                    "login:coros" -> openAccount("coros")
                    "login:keep" -> openAccount("keep")
                    "mealShot" -> askMealPhoto()
                    "aiSettings" -> openAiSettings()
                    "aiToggle" -> toggleAi()
                    "simpleToggle" -> toggleSimple()
                    "corosSleep" -> askCorosSleep()
                    "login" -> showLoginPicker()
                    "mi" -> showMiPanel()
                    "hw" -> showHwPanel()
                    "bodySource" -> showBodySourcePicker()
                    "planRefresh" -> refreshPlan()
                    "gpx" -> pickGpx()
                    "attach" -> pickAttachment(null)
                    "amap" -> showAmapDialog()
                    "clips" -> showClips()
                    "update" -> checkUpdate(silent = false)
                    "dbExport" -> doExportData()
                    "dbImport" -> doImportData()
                    "diag" -> showDiag()
                    // 带参数的动作放最后：`else` 必须是 when 的最后一个分支
                    else -> if (name.startsWith("attach:")) {
                        pickAttachment(name.removePrefix("attach:"))
                    } else if (name.startsWith("openUrl:")) {
                        openLocalFile(name.removePrefix("openUrl:"))
                    } else {
                        toast("未知操作：$name")
                    }
                }
            }
        }
    }

    // ------------------------------------------------------------ 底部导航
    // 教练排第二位：它是每天要点的东西，不该比「历史」还靠后。
    private val TAB_NAMES = arrayOf("今日", "教练", "历史", "我的")
    private val TAB_URLS = arrayOf("/", "/ai", "/history", "/me")
    private val TAB_ICONS = intArrayOf(
        R.drawable.ic_tab_today, R.drawable.ic_tab_coach,
        R.drawable.ic_tab_history, R.drawable.ic_tab_me)

    // 语义化索引：顺序调过一次（教练 3→2），别再往逻辑里写裸数字
    private val TAB_TODAY = 0
    private val TAB_COACH = 1
    private val TAB_HISTORY = 2
    private val TAB_ME = 3

    private fun buildTabBar(): LinearLayout {
        val nav = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(0xFF080D15.toInt())
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
            val c = if (on) 0xFF4DA3FF.toInt() else 0xFF4E6076.toInt()
            tabIcons[i].setColorFilter(c)
            tabLabels[i].setTextColor(c)
            tabLabels[i].setTypeface(
                android.graphics.Typeface.DEFAULT,
                if (on) android.graphics.Typeface.BOLD else android.graphics.Typeface.NORMAL)
            tabRoots[i].setBackgroundColor(if (on) 0x243B82F6.toInt() else 0x00000000)
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
            path.startsWith("/ai") -> TAB_COACH
            path.startsWith("/me") || path.startsWith("/files") ||
                path.startsWith("/privacy") || path.startsWith("/sports") -> TAB_ME
            path.startsWith("/history") || path.startsWith("/view") -> TAB_HISTORY
            else -> TAB_TODAY
        }
        if (t != activeTab) {
            activeTab = t
            updateTabs()
        }
    }

    // DatePicker 的月份是 0-based；统一转成 YYYY-MM-DD 再传给 Python 侧校验。
    /**
     * 后台由近到远补齐历史，标题栏小字显示进度。
     *
     * 以前这是「我的」里的一个按钮：点开一个弹窗、选区间，跑完只回一句
     * 「跳过 90 天缓存」—— 用户的实际感受是「点了没有任何效果」。既然它本质是
     * 必须跑完才有意义的事，就干脆不让人点：**每次刷新顺手往前挪一段**。
     *
     * Python 侧用游标 `backfill_cursor` 记住「下次从哪天往前补」，默认今天，
     * 一段一年；终点是**数据源里最早的那条活动**（平台会告诉我们），而不是
     * 硬退 8 年——账号才注册两年时，退到 2018 只会白拉两年的空数据。
     *
     * 每次刷新最多推 3 段：这本来就该是后台慢慢补的事，一次把八年全拉完，
     * 用户看到的就是「一直在拉」。没补完的，下次刷新接着补。
     */
    private fun runBackfill() {
        if (backfilling) return
        backfilling = true
        thread {
            var seg = 0
            var fetched = 0
            var lastErr = ""
            var needLogin = false
            var done = false
            var earliest = ""
            while (!done && seg < 3) {             // 每次刷新最多 3 段，剩下的下次接着补
                val r = apiCall("backfill_next")
                if (!r.optBoolean("ok")) {
                    lastErr = r.optString("error")
                    needLogin = r.optBoolean("need_login") || r.optBoolean("need_mfa")
                    break
                }
                done = r.optBoolean("done")
                fetched += r.optInt("fetched")
                if (r.optString("earliest").isNotEmpty()) earliest = r.optString("earliest")
                if (!done) {
                    seg++
                    val a = r.optString("start")
                    val b = r.optString("end")
                    Log.i(TAG, "backfill seg=$seg $a~$b fetched=${r.optInt("fetched")} " +
                        "skipped=${r.optInt("skipped")}")
                    ui {
                        subtitle.text = "补齐历史 第 $seg 段：$a ~ $b（新拉 ${r.optInt("fetched")} 天）"
                        // 一段要跑一分多钟：停在「历史」页时每补完一段就刷一次，
                        // 否则用户盯着的是几分钟前的列表，以为没拉到东西。
                        if (activeTab == TAB_HISTORY) web.reload()
                    }
                }
            }
            backfilling = false
            Log.i(TAG, "backfill done seg=$seg fetched=$fetched err=$lastErr")

            ui {
                updateSubtitle()
                // 后台任务遇到会话失效不弹登录：用户可能正在看别的页面，
                // 下一次主动刷新时自然会提示。
                if (needLogin) return@ui
                if (seg > 0 && fetched > 0) {
                    val tail = if (done && earliest.isNotEmpty())
                        "，已到最早记录 $earliest" else "，下次刷新继续补齐"
                    toast("已补 $seg 段历史：新拉 $fetched 天$tail")
                }
                thread {
                    setStatus(apiCall("status"))
                    ui { reload() }
                }
            }
        }
    }

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

    // ------------------------------------------------------------ 启动
    private fun bootServer() {
        val r = apiCall("start_server", 0)
        if (!r.optBoolean("ok")) {
            fatal("本地服务没起来", r.optString("error"))
            return
        }
        port = r.optInt("port")
        ui { goTab(0) }
        // 冷启动阶段接到的 GPX：服务已就绪，这里补一次导入
        pendingGpx?.let { u ->
            pendingGpx = null
            importGpx(u)
        }

        // 首次装完是空库，直接把登录框弹出来，别让人对着一页「还没有东西」猜
        val s = apiCall("status")
        setStatus(s)
        val c = apiCall("coros_status")
        if (!s.optBoolean("logged_in") && !c.optBoolean("logged_in")) {
            ui { showLoginPicker() }
        }
        // 静默查一次新版本：延迟 6 秒，避开首屏渲染与登录框；24 小时节流
        ui { web.postDelayed({ checkUpdate(silent = true) }, 6000) }
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
        val kMobile = secrets.keepMobile()
        val hasG = !gEmail.isNullOrBlank() && !gPwd.isNullOrBlank()
        val hasC = !cEmail.isNullOrBlank()
        val hasK = !kMobile.isNullOrBlank()
        if (!hasG && !hasC && !hasK) {
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
        val kMobile = secrets.keepMobile() ?: ""
        val kPwd = secrets.keepPassword() ?: ""
        ui {
            pull.setRefreshing(true)
            subtitle.text = "正在同步…"
        }
        thread {
            val msgs = mutableListOf<String>()
            var okAny = false
            var garminDead = false
            var garminMfa = false
            var corosDead = false
            var keepDead = false
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
                    // 续期需要邮箱验证码 ≠ 密码错：弹验证码框，别让人以为登录失效
                    r.optBoolean("need_mfa") -> garminMfa = true
                    r.optBoolean("need_login") -> garminDead = true
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
            if (kMobile.isNotBlank()) {
                // 账密从 Keystore 传过去：token 过期时 Python 侧会静默重登
                val r = apiCall("keep_sync", 30, kMobile, kPwd)
                when {
                    r.optBoolean("ok") -> {
                        okAny = true
                        msgs.add("Keep ${r.optInt("activities")} 条")
                    }
                    r.optBoolean("need_login") -> keepDead = true
                    else -> err = (if (err.isEmpty()) "" else "$err · ") + "Keep：" + r.optString("error")
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
                            if (keepDead) toast("Keep 登录已过期，请在「我的」里重登")
                            reload()
                            // 每次刷新（含刚登录）都在后台往前补一段；
                            // 已经补完的库会在第一次调用时立即返回，不发请求。
                            runBackfill()
                        }
                    }
                } else {
                    setStatus(apiCall("status"))
                    when {
                        garminMfa -> {
                            toast("佳明会话续期需要邮箱验证码")
                            askMfa()
                        }
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
                        keepDead -> {
                            toast("Keep 登录已过期，请重新登录")
                            askKeepLogin()
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
                    setTextColor(0xFFE8F0FB.toInt())
                    setTypeface(typeface, android.graphics.Typeface.BOLD)
                })
                addView(TextView(this@MainActivity).apply {
                    text = sub
                    textSize = 11.5f
                    setTextColor(0xFF7C8FA8.toInt())
                })
            }, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
            box.addView(r)
        }

        val g = secrets.email()
        val c = secrets.corosEmail()
        row(R.drawable.ic_logo_garmin, "Garmin 佳明国区",
            if (g.isNullOrBlank()) "邮箱密码 + 邮箱验证码" else "已登录 $g · 查看账号 / 更换"
        ) { openAccount("garmin") }
        box.addView(View(this).apply { setBackgroundColor(0xFF16202F.toInt()) },
            LinearLayout.LayoutParams(MATCH_PARENT, 1))
        row(R.drawable.ic_logo_coros, "COROS 高驰",
            if (c.isNullOrBlank()) "邮箱密码（Training Hub）· token 自动续期" else "已登录 $c · 查看账号 / 更换"
        ) { openAccount("coros") }

        AlertDialog.Builder(this)
            .setTitle("账号登录")
            .setMessage("选择手表数据来源；两个账号可以都登，数据写进同一套看板。凭据只加密存在本机。")
            .setView(box)
            .setNegativeButton("关闭", null)
            .show()
    }

    // ------------------------------------------------------------ 账号卡片
    /**
     * 已登录时点账号行：先看账号卡片，**不再弹登录框**。
     *
     * 以前这里一律弹「输入邮箱密码」，已经登录的人被要求再输一遍，纯属添堵。
     * 现在卡片只读展示账号、设备、会话状态与本地覆盖，换账号/退出各给一个按钮；
     * 只有确实没登录（或会话失效）才回到登录框。
     */
    private fun openAccount(source: String) {
        busy(true)
        thread {
            val r = apiCall("account_info", source)
            // 高驰账号卡片要显示睡眠授权状态，并留一个补授权/同步的入口
            val mcp = if (source == "coros") apiCall("coros_mcp_status") else null
            ui {
                busy(false)
                if (!r.optBoolean("ok") || !r.optBoolean("logged_in")) {
                    when (source) {
                        "coros" -> askCorosLogin()
                        "keep" -> askKeepLogin()
                        else -> askGarminLogin()
                    }
                    return@ui
                }
                showAccountCard(source, r, mcp)
            }
        }
    }

    private fun showAccountCard(source: String, r: JSONObject, mcp: JSONObject? = null) {
        val lines = mutableListOf<String>()
        r.optString("account").takeIf { it.isNotBlank() }?.let { lines.add("账号    $it") }
        r.optString("device").takeIf { it.isNotBlank() }?.let { lines.add("设备    $it") }
        r.optString("region").takeIf { it.isNotBlank() }?.let { lines.add("区域    $it") }
        r.optString("session").takeIf { it.isNotBlank() }?.let { lines.add("会话    $it") }
        val acts = r.optInt("activities")
        val first = r.optString("first")
        val last = r.optString("last")
        if (acts > 0) lines.add("数据    $acts 条活动 · $first ~ $last")
        if (r.optInt("days") > 0) lines.add("日汇总  ${r.optInt("days")} 天")
        r.optString("last_sync").takeIf { it.isNotBlank() }?.let { lines.add("同步    $it") }
        val sleepAuthed = mcp?.optBoolean("authorized") == true
        if (source == "coros" && mcp != null)
            lines.add("睡眠    " + if (sleepAuthed) "已授权（官方 MCP）" else "未授权（可选）")
        val builder = AlertDialog.Builder(this)
            .setTitle(r.optString("label", "账号"))
            .setMessage(lines.joinToString("\n"))
            .setPositiveButton("更换其他账号") { _, _ ->
                when (source) {
                    "coros" -> askCorosLogin()
                    "keep" -> askKeepLogin()
                    else -> askGarminLogin()
                }
            }
        if (source == "coros" && mcp != null) {
            // 高驰多一个睡眠入口：登录时点过「暂不」的人在这里补授权
            builder.setNeutralButton(if (sleepAuthed) "同步睡眠" else "授权睡眠") { _, _ ->
                if (sleepAuthed) doCorosMcpSync() else askCorosSleep()
            }
            builder.setNegativeButton("退出登录") { _, _ -> confirmLogout(source) }
        } else {
            builder.setNeutralButton("退出登录") { _, _ -> confirmLogout(source) }
            builder.setNegativeButton("关闭", null)
        }
        builder.show()
    }

    private fun confirmLogout(source: String) {
        val who = when (source) {
            "coros" -> "高驰"
            "keep" -> "Keep"
            else -> "佳明"
        }
        AlertDialog.Builder(this)
            .setTitle("退出$who 账号")
            .setMessage("只清除登录状态，本机历史数据不会被删除。退出后可以换一个${who}账号登录。")
            .setPositiveButton("退出") { _, _ ->
                busy(true)
                thread {
                    val r = apiCall(when (source) {
                        "coros" -> "coros_logout"
                        "keep" -> "keep_logout"
                        else -> "garmin_logout"
                    })
                    ui {
                        busy(false)
                        if (!r.optBoolean("ok")) {
                            toast("退出失败：${r.optString("error")}")
                            return@ui
                        }
                        when (source) {
                            "coros" -> secrets.clearCoros()
                            "keep" -> secrets.clearKeep()
                            else -> secrets.clear()
                        }
                        toast("已退出${who}账号")
                        thread {
                            setStatus(apiCall("status"))
                            ui { reload() }
                        }
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    /**
     * 密码框下面的「显示密码」开关。
     *
     * 密码输错一次就要整栏重打，而「怕被旁人看到」在这个场景基本不成立
     * （登录时旁边通常没人，而且密码框默认仍是掩码）。默认不勾，勾上立刻
     * 明文并保持光标在末尾——用户刚输入完就能核对是哪一个字符错了。
     */
    private fun addShowPassword(box: LinearLayout, field: EditText) {
        val cb = CheckBox(this).apply {
            text = "显示密码"
            textSize = 13f
            setPadding(0, dp(2), 0, dp(6))
        }
        cb.setOnCheckedChangeListener { _, on ->
            field.inputType = if (on)
                InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
            else
                InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            field.setSelection(field.text.length)
        }
        box.addView(cb)
    }

    /**
     * 登录中的小卡片：一张窄卡片 + 转圈 + 正在登录哪个账号。
     *
     * 为什么不复用顶部那条 2dp 进度线：登录要走一次 SSO 往返，手机上 3~15 秒
     * 很常见；细线在深色页面里几乎看不见，用户以为没点中就会再点一次，于是
     * 并发两次登录。卡片明确写着「正在登录」和账号，还允许取消（请求本身
     * 不能中断，但至少能把卡片收掉）。
     */
    private fun showLoginCard(title: String, account: String): AlertDialog {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dp(24), dp(20), dp(24), dp(4))
        }
        box.addView(ProgressBar(this).apply { isIndeterminate = true })
        box.addView(TextView(this).apply {
            text = account
            textSize = 13f
            gravity = Gravity.CENTER
            setTextColor(0xFF939FAD.toInt())
            setPadding(0, dp(10), 0, 0)
        })
        val d = AlertDialog.Builder(this)
            .setTitle(title)
            .setView(box)
            .setNegativeButton("取消") { _, _ -> }
            .setCancelable(false)
            .create()
        d.show()
        return d
    }

    private fun askGarminLogin(email: String? = null, pwd: String? = null,
                               err: String? = null) {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eEmail = EditText(this).apply {
            hint = "Garmin 国区邮箱"
            inputType = InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS
            setText(email ?: secrets.email() ?: "")
        }
        val ePwd = EditText(this).apply {
            hint = "密码"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(pwd ?: "")
        }
        box.addView(eEmail)
        box.addView(ePwd)
        addShowPassword(box, ePwd)

        // 失败重开时把上一次的错误写在最上面：用户在同一个框里改，不用回忆刚才报了什么
        val tip = if (err.isNullOrBlank()) "账号只加密存在本机，不会上传到任何服务器。"
        else "上次登录失败：$err\n\n账号只加密存在本机。改了再试一次即可。"
        AlertDialog.Builder(this)
            .setTitle(if (err.isNullOrBlank()) "登录佳明国区" else "重新登录佳明国区")
            .setMessage(tip)
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
        val card = showLoginCard("正在登录佳明", email)
        thread {
            val r = apiCall("login", email, pwd)
            ui {
                card.dismiss()
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
                    else -> askGarminLogin(email, pwd,
                                           r.optString("error").ifBlank { "未知错误" })
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
    private fun askCorosLogin(email: String? = null, pwd: String? = null,
                              region: String = "asia", err: String? = null) {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eEmail = EditText(this).apply {
            hint = "高驰账号（邮箱或手机号）"
            inputType = InputType.TYPE_CLASS_TEXT
            setText(email ?: secrets.corosEmail() ?: "")
        }
        val ePwd = EditText(this).apply {
            hint = "密码"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(pwd ?: "")
        }
        // 区域：默认国内；用单选对话框选，避免 Spinner 样式各机型不一致
        val regions = arrayOf("国内", "欧洲", "美国")
        val regionKeys = arrayOf("asia", "eu", "us")
        // 失败重开时保留上次选的区域，别让人重新选一遍
        var regionIdx = regionKeys.indexOf(region).coerceAtLeast(0)
        val vRegion = TextView(this).apply {
            text = "账号区域：" + regions[regionIdx]
            textSize = 14f
            setTextColor(0xFFB7C7DD.toInt())
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
        addShowPassword(box, ePwd)
        box.addView(vRegion)
        val tip = if (err.isNullOrBlank())
            "与高驰手表 App 同一个账号，可用邮箱或手机号。凭据只加密存在本机；token 过期后自动续登。\n区域要与注册时一致，国内账号用「国内」。"
        else "上次登录失败：$err\n\n区域要与注册时一致，国内账号用「国内」。"
        AlertDialog.Builder(this)
            .setTitle(if (err.isNullOrBlank()) "登录 COROS 高驰" else "重新登录 COROS 高驰")
            .setMessage(tip)
            .setView(box)
            .setNegativeButton("取消", null)
            .setPositiveButton("登录") { _, _ ->
                val e = eEmail.text.toString().trim()
                val p = ePwd.text.toString()
                if (e.isBlank() || p.isBlank()) toast("账号和密码都要填")
                else doCorosLogin(e, p, regionKeys[regionIdx])
            }
            .show()
    }

    // ------------------------------------------------------------ Keep
    /**
     * Keep 的登录框。Keep 没有长效票据，token 过期必须账密重登，
     * 所以密码交给 Keystore 保管，Python 侧每次同步时收参数。
     */
    private fun askKeepLogin(mobile: String = "", pwd: String = "", err: String? = null) {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eMobile = EditText(this).apply {
            hint = "Keep 手机号"
            inputType = InputType.TYPE_CLASS_PHONE
            setText(mobile.ifBlank { secrets.keepMobile() ?: "" })
        }
        val ePwd = EditText(this).apply {
            hint = "密码"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(pwd)
        }
        box.addView(eMobile)
        box.addView(ePwd)
        addShowPassword(box, ePwd)
        val tip = if (err.isNullOrBlank())
            "与 Keep App 同一个手机号。只同步运动记录摘要（时间、距离、时长、心率），" +
                "没有轨迹和分圈；凭据用系统 Keystore 加密，只存本机。"
        else "上次登录失败：$err"
        AlertDialog.Builder(this)
            .setTitle(if (err.isNullOrBlank()) "登录 Keep" else "重新登录 Keep")
            .setMessage(tip)
            .setView(box)
            .setNegativeButton("取消", null)
            .setPositiveButton("登录") { _, _ ->
                val m = eMobile.text.toString().trim()
                val p = ePwd.text.toString()
                if (m.isBlank() || p.isBlank()) toast("手机号和密码都要填")
                else doKeepLogin(m, p)
            }
            .show()
    }

    private fun doKeepLogin(mobile: String, pwd: String) {
        val card = showLoginCard("正在登录 Keep", mobile)
        thread {
            val r = apiCall("keep_login", mobile, pwd)
            ui {
                card.dismiss()
                if (r.optBoolean("ok")) {
                    secrets.saveKeep(mobile, pwd)
                    toast("Keep 登录成功")
                    doSync()
                } else {
                    askKeepLogin(mobile, pwd, r.optString("error").ifBlank { "未知错误" })
                }
            }
        }
    }

    // ------------------------------------------------------------ 拍照记饮食
    /** 拍照识热量：图片在原生侧压缩后交给 Python，识别结果确认后才落库。 */
    private var mealPhotoUri: Uri? = null

    /** 「我的 → AI 模型服务」：复用教练页右上角那个设置抽屉（/ai#settings）。 */
    private fun openAiSettings() {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        web.loadUrl("http://127.0.0.1:$port/ai#settings")
    }

    /** 「我的 → 简洁模式」：先把不用的模块藏起来，随时可以关回来。 */
    private fun toggleSimple() {
        thread {
            val r = apiCall("simple_toggle")
            ui {
                if (!r.optBoolean("ok")) {
                    toast("切换失败：${r.optString("error")}")
                    return@ui
                }
                toast(if (r.optBoolean("simple_mode")) "已切到简洁模式" else "已恢复完整模式")
                reload()
            }
        }
    }

    /** 行尾开关：只翻 AI 总开关，不动 provider / key，改完刷新当前页。 */
    private fun toggleAi() {
        thread {
            val r = apiCall("ai_toggle")
            ui {
                if (!r.optBoolean("ok")) {
                    toast("切换失败：${r.optString("error")}")
                    return@ui
                }
                toast(if (r.optBoolean("ai_enabled")) "AI 已开启" else "AI 已关闭")
                reload()
            }
        }
    }

    private fun askMealPhoto() {
        AlertDialog.Builder(this)
            .setTitle("记饮食")
            .setItems(arrayOf("拍照", "从相册选", "直接描述（吃完了没拍）")) { _, which ->
                when (which) {
                    0 -> takeMealPhoto()
                    1 -> pickMealPhoto()
                    else -> askMealText()
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    /** 吃完了才想起来没拍照：用文字描述同一套估算，字段和拍照完全一致。 */
    private fun askMealText() {
        val input = android.widget.EditText(this).apply {
            hint = "例如：一碗牛肉面，两个鸡蛋，一杯无糖豆浆"
            setTextSize(14f)
            minLines = 3
            gravity = android.view.Gravity.TOP
        }
        val pad = dp(16)
        val wrap = android.widget.FrameLayout(this).apply {
            setPadding(pad, dp(8), pad, 0)
            addView(input, android.widget.FrameLayout.LayoutParams(
                android.widget.FrameLayout.LayoutParams.MATCH_PARENT, dp(120)))
        }
        AlertDialog.Builder(this)
            .setTitle("直接描述这一餐")
            .setMessage("写清吃了什么、大概多少；份量说得越具体，估得越准。")
            .setView(wrap)
            .setPositiveButton("估算") { _, _ ->
                val desc = input.text.toString().trim()
                if (desc.isBlank()) {
                    toast("先写一句吃了什么")
                } else {
                    doMealEstimateText(desc)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun doMealEstimateText(desc: String) {
        val card = showLoginCard("正在估算", "让模型按描述估一下热量…")
        thread {
            // 图片参数留空：后端看到「没图但有描述」就走纯文字估算
            val r = apiCall("meal_estimate", "", "image/jpeg", desc)
            ui {
                card.dismiss()
                if (!r.optBoolean("ok")) {
                    toast("估算失败：${r.optString("error")}")
                    return@ui
                }
                showMealResult(r, desc)
            }
        }
    }

    private fun takeMealPhoto() {
        try {
            val values = ContentValues().apply {
                put(MediaStore.Images.Media.DISPLAY_NAME,
                    "meal_${System.currentTimeMillis()}.jpg")
                put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
            }
            val uri = contentResolver.insert(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
            if (uri == null) {
                toast("无法创建照片文件")
                return
            }
            mealPhotoUri = uri
            startActivityForResult(
                Intent(MediaStore.ACTION_IMAGE_CAPTURE)
                    .putExtra(MediaStore.EXTRA_OUTPUT, uri),
                REQ_MEAL_SHOT,
            )
        } catch (t: Throwable) {
            toast("打不开相机：${t.message}")
        }
    }

    private fun pickMealPhoto() {
        try {
            startActivityForResult(
                Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "image/*"
                },
                REQ_MEAL_PICK,
            )
        } catch (t: Throwable) {
            toast("打不开相册：${t.message}")
        }
    }

    /** 先问一句描述再识别：照片看不清份量时，一句话比让模型瞎猜准得多。 */
    private fun doMealEstimate(uri: Uri) {
        val input = android.widget.EditText(this).apply {
            hint = "可留空。例如：一碗牛肉面，汤没喝完；两块炸鸡"
            setTextSize(14f)
            minLines = 2
            gravity = android.view.Gravity.TOP
        }
        val pad = dp(16)
        val wrap = android.widget.FrameLayout(this).apply {
            setPadding(pad, dp(8), pad, 0)
            addView(input, android.widget.FrameLayout.LayoutParams(
                android.widget.FrameLayout.LayoutParams.MATCH_PARENT, dp(96)))
        }
        AlertDialog.Builder(this)
            .setTitle("补充一句（可选）")
            .setMessage("照片看不清份量时写一句更准：主食多少、有没有喝汤、几个鸡蛋。留空就直接按照片估。")
            .setView(wrap)
            // 只留一个识别按钮：输入框留空时，「识别」和原来的「直接识别」
            // 走的是同一条路径，两个按钮只是让人犹豫点哪个。
            .setPositiveButton("识别") { _, _ ->
                doMealEstimate(uri, input.text.toString().trim())
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun doMealEstimate(uri: Uri, note: String) {
        val card = showLoginCard("正在识别", "让模型看看这餐有多少热量…")
        thread {
            val pair = mealImageBase64(uri)
            if (pair == null) {
                ui { card.dismiss(); toast("读不出这张照片") }
                return@thread
            }
            val r = apiCall("meal_estimate", pair.first, pair.second, note)
            ui {
                card.dismiss()
                if (!r.optBoolean("ok")) {
                    toast("识别失败：${r.optString("error")}")
                    return@ui
                }
                showMealResult(r, note)
            }
        }
    }

    private fun showMealResult(j: JSONObject, note: String = "") {
        val items = j.optJSONArray("items") ?: JSONArray()
        if (items.length() == 0) {
            toast(if (note.isBlank())
                "没认出食物。点「拍照记饮食」再加一句描述（主食多少、几个菜）会更准"
            else "还是没认出食物，换个角度再拍一张")
            return
        }
        val total = j.optJSONObject("total") ?: JSONObject()
        val sb = StringBuilder()
        for (i in 0 until items.length()) {
            val it = items.optJSONObject(i) ?: continue
            sb.append("· ").append(it.optString("name"))
                .append("  ").append(it.optInt("kcal")).append(" kcal")
            val p = it.optDouble("protein_g", 0.0)
            if (p > 0) sb.append("（蛋白 ").append(p).append(" g）")
            sb.append("\n")
        }
        val note = j.optString("note")
        if (note.isNotBlank()) sb.append("\n").append(note)
        AlertDialog.Builder(this)
            .setTitle("识别结果 · ${total.optInt("kcal")} kcal")
            .setMessage(sb.toString().trim())
            .setPositiveButton("记下") { _, _ -> saveMeal(items) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun saveMeal(items: JSONArray) {
        thread {
            val r = apiCall("meal_add", items.toString())
            ui {
                if (!r.optBoolean("ok")) {
                    toast("保存失败：${r.optString("error")}")
                    return@ui
                }
                val kcal = r.optJSONObject("total")?.optInt("kcal") ?: 0
                toast("已记下 $kcal kcal，一图流和看板已更新")
                reload()
            }
        }
    }

    /** 压缩到 1280 px / JPEG 80：够模型看，又不会把几 MB 原图塞进桥。 */
    private fun mealImageBase64(uri: Uri, maxSide: Int = 1280): Pair<String, String>? {
        return try {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            contentResolver.openInputStream(uri)?.use {
                BitmapFactory.decodeStream(it, null, bounds)
            }
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
            var sample = 1
            while (maxOf(bounds.outWidth, bounds.outHeight) / sample > maxSide * 2) {
                sample *= 2
            }
            val src = contentResolver.openInputStream(uri)?.use {
                BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply {
                    inSampleSize = sample
                })
            } ?: return null
            val scale = maxSide.toFloat() / maxOf(src.width, src.height)
            val out = if (scale < 1f)
                Bitmap.createScaledBitmap(src, (src.width * scale).toInt(),
                    (src.height * scale).toInt(), true)
            else src
            val bos = ByteArrayOutputStream()
            out.compress(Bitmap.CompressFormat.JPEG, 80, bos)
            if (out !== src) out.recycle()
            src.recycle()
            Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP) to "image/jpeg"
        } catch (t: Throwable) {
            Log.w(TAG, "meal image", t)
            null
        }
    }

    private fun doCorosLogin(email: String, pwd: String, region: String) {
        val card = showLoginCard("正在登录高驰", email)
        thread {
            val r = apiCall("coros_login", email, pwd, region)
            ui {
                card.dismiss()
                if (r.optBoolean("ok")) {
                    secrets.saveCoros(email, pwd)
                    toast("高驰登录成功（${r.optString("user_id")}）")
                    // 活动/HRV/静息心率已经能同步；睡眠还差一次官方 MCP 的 OAuth。
                    // 直接在这里问意向，省掉「我的」里单独那个睡眠按钮。
                    askSleepAfterLogin()
                } else {
                    // 失败直接带着错误重开登录框：区域、账号、密码都还在，改一处再试
                    askCorosLogin(email, pwd, region,
                                  r.optString("error").ifBlank { "未知错误" })
                }
            }
        }
    }

    /**
     * 登录成功后的睡眠授权意向。
     *
     * 活动、HRV、静息心率用账号密码就能同步；睡眠不在 Training Hub 的 REST 里，
     * 必须再走一次官方 MCP 的 OAuth。以前这件事占「我的」里单独一行，用户登录
     * 完还得再找一次按钮——现在登录时就问清楚：同意直接跳授权页，不同意先同步
     * 活动，之后账号卡片里还能补。
     */
    private fun askSleepAfterLogin() {
        thread {
            val st = apiCall("coros_mcp_status")
            ui {
                if (st.optBoolean("authorized")) {
                    doSync()
                    return@ui
                }
                AlertDialog.Builder(this)
                    .setTitle("顺便授权睡眠数据？")
                    .setMessage(
                        "活动、HRV、静息心率已经能同步了；睡眠不在高驰的网页接口里，" +
                            "要再用官方 MCP 授权一次才能读到睡眠评分和深/浅/REM。\n\n" +
                            "点「去授权」会打开高驰登录页，登录并同意即可；" +
                            "不会把手机上的高驰 App 挤下线，数据也不经过我们的服务器。"
                    )
                    .setPositiveButton("去授权") { _, _ -> startCorosMcpAuth() }
                    .setNegativeButton("暂不") { _, _ -> doSync() }
                    .setOnCancelListener { doSync() }
                    .show()
            }
        }
    }

    // --------------------------------------------------- 高驰睡眠（官方 MCP）
    /**
     * 睡眠不在 Training Hub 的 REST 里（那里只有 HRV / 静息心率 / 负荷），
     * 只能走官方 MCP 网关。授权是 OAuth：打开高驰登录页、用户点同意，
     * 回调地址由 CorosLoginActivity 拦下来，code 交给 Python 换 token。
     *
     * 和移动端 API 那条路的最大区别：**不会把用户手机上的高驰 App 挤下线**。
     */
    private fun askCorosSleep() {
        busy(true)
        thread {
            val st = apiCall("coros_mcp_status")
            ui {
                busy(false)
                val authed = st.optBoolean("authorized")
                val left = st.optInt("expires_in_min")
                AlertDialog.Builder(this)
                    .setTitle("高驰睡眠")
                    .setMessage(
                        if (authed)
                            "已授权（${st.optString("region_cn")}，access token 还剩 $left 分钟，" +
                                "到期自动续期）。\n\n睡眠评分、主睡眠时长与深/浅/REM 会并进日汇总，" +
                                "一图流和恢复度都会用上。"
                        else
                            "高驰的睡眠不在网页接口里，要用官方 MCP 授权才能读。\n\n" +
                                "点「去授权」会打开高驰登录页，登录并同意即可。" +
                                "不会把你手机上的高驰 App 挤下线，数据也不经过我们的服务器。"
                    )
                    .setPositiveButton(if (authed) "同步睡眠" else "去授权") { _, _ ->
                        if (authed) doCorosMcpSync() else startCorosMcpAuth()
                    }
                    .apply {
                        if (authed) setNeutralButton("重新授权") { _, _ -> startCorosMcpAuth() }
                        setNegativeButton("关闭", null)
                    }
                    .show()
            }
        }
    }

    private fun startCorosMcpAuth() {
        busy(true)
        thread {
            // 区域跟随高驰账号（国内/欧洲/美国），不再在这里写死
            val r = apiCall("coros_mcp_login_url")
            ui {
                busy(false)
                if (r.optBoolean("ok"))
                    startActivityForResult(
                        Intent(this, CorosLoginActivity::class.java)
                            .putExtra("url", r.optString("url")),
                        7302
                    )
                else toast("打不开高驰授权页：${r.optString("error")}")
            }
        }
    }

    private fun finishCorosMcpAuth(callback: String) {
        busy(true)
        thread {
            val r = apiCall("coros_mcp_finish", callback)
            ui {
                if (!r.optBoolean("ok")) {
                    busy(false)
                    toast("授权失败：${r.optString("error")}")
                } else {
                    toast("高驰授权成功，正在同步睡眠…")
                    // 从登录流程过来时活动还没同步过，睡眠收尾后补一次
                    doCorosMcpSync(thenSync = true)
                }
            }
        }
    }

    private fun doCorosMcpSync(thenSync: Boolean = false) {
        busy(true)
        thread {
            val r = apiCall("coros_mcp_sleep", 30)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    val n = r.optInt("sleep_days")
                    if (n > 0) {
                        toast("已同步 $n 天睡眠")
                    } else {
                        // 授权没问题却没数据时，把接口原样返回给用户看：
                        // 可能账号确实没有睡眠，也可能是字段格式又变了
                        val raw = r.optString("raw")
                        AlertDialog.Builder(this)
                            .setTitle("授权正常，但没有睡眠数据")
                            .setMessage(
                                "高驰返回的内容里没有可识别的睡眠记录。\n\n" +
                                    "常见原因：手表没戴过夜 / 没开睡眠监测 / 账号里这段日期确实为空。\n\n" +
                                    (if (raw.isBlank()) "（接口没有返回任何文本）"
                                     else "接口原文（可截图发我）：\n$raw")
                            )
                            .setPositiveButton("知道了", null)
                            .show()
                    }
                    reload()
                } else {
                    toast("同步睡眠失败：${r.optString("error")}")
                }
                if (thenSync) doSync()
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
            setTextColor(0xFFB7C7DD.toInt())
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
    /** 后台补全历史是否正在跑：下拉刷新连续触发时避免并发两轮。 */
    private var backfilling = false

    /** 当前素材对话框。重建前必须先关掉它，否则会一层层叠起来。 */
    private var clipDialog: Dialog? = null
    /** 标题在列表里要动态改成「已选 N 个」，所以单独留个引用。 */
    private var clipTitle: TextView? = null
    private var clipSelecting = false
    private val clipChecked = linkedSetOf<String>()

    private fun renderClipList() {
        // 每次重建都先关掉上一版：不关就会一层层叠上去，
        // 删完素材还能看见旧列表 —— 用户说的「多保留历史界面」就是这个。
        clipDialog?.dismiss()
        clipDialog = null
        clipTitle = null
        val arr = clipArray
        if (arr.length() == 0) {
            clipSelecting = false
            clipChecked.clear()
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
            setPadding(dp(8), dp(4), dp(8), dp(8))
        }
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: JSONObject()
            box.addView(clipRow(o))
            if (i < arr.length() - 1) {
                box.addView(View(this).apply { setBackgroundColor(0xFF16202F.toInt()) },
                    LinearLayout.LayoutParams(MATCH_PARENT, 1).apply {
                        leftMargin = dp(66); topMargin = dp(2); bottomMargin = dp(2)
                    })
            }
        }

        // 固定高度的 Dialog + 自己的 ScrollView。
        // 旧版走 AlertDialog.setView()，customPanel 在不少机型上会被压到只剩
        // 七八行 —— 列表看着像「只显示最新的 8 个」，其实是滚不动。
        // 这里把中间那块做成明确可滚（weight=1），标题写总数，一眼能确认条数。
        val title = TextView(this).apply {
            text = if (clipSelecting) "已选 ${clipChecked.size} 个" else "素材（${arr.length()}）"
            textSize = 17f
            setTypeface(typeface, android.graphics.Typeface.BOLD)
            setTextColor(0xFFE8F0FB.toInt())
            setPadding(dp(18), dp(14), dp(18), dp(10))
        }
        clipTitle = title

        val sv = ScrollView(this).apply {
            isFillViewport = false
            addView(box, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        }

        val btnRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END
            setPadding(dp(8), dp(4), dp(8), dp(6))
        }
        fun btn(txt: String, color: Int, cb: () -> Unit): TextView =
            TextView(this).apply {
                text = txt
                textSize = 14f
                setTextColor(color)
                setPadding(dp(14), dp(10), dp(14), dp(10))
                isClickable = true
                setOnClickListener { cb() }
            }
        btnRow.addView(btn("关闭", 0xFFB7C7DD.toInt()) { clipDialog?.dismiss() })
        btnRow.addView(btn(if (clipSelecting) "退出选择" else "批量管理",
            0xFF2E5A8A.toInt()) {
            clipSelecting = !clipSelecting
            if (!clipSelecting) clipChecked.clear()
            renderClipList()
        })
        btnRow.addView(btn(if (clipSelecting) "删除选中" else "添加",
            0xFF4DA3FF.toInt()) {
            if (clipSelecting) {
                deleteCheckedClips()
            } else {
                clipDialog?.dismiss()
                pickMedia()
            }
        })

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(16).toFloat()
                setColor(0xFF0F1622.toInt())
            }
        }
        val line = { View(this).apply { setBackgroundColor(0xFF16202F.toInt()) } }
        root.addView(title, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        root.addView(line(), LinearLayout.LayoutParams(MATCH_PARENT, 1))
        root.addView(sv, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        root.addView(line(), LinearLayout.LayoutParams(MATCH_PARENT, 1))
        root.addView(btnRow, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))

        val dlg = Dialog(this)
        dlg.requestWindowFeature(Window.FEATURE_NO_TITLE)
        dlg.setContentView(root)
        val dm = resources.displayMetrics
        dlg.window?.setBackgroundDrawableResource(android.R.color.transparent)
        dlg.window?.setLayout((dm.widthPixels * 0.94f).toInt(),
            (dm.heightPixels * 0.8f).toInt())
        dlg.setOnDismissListener {
            if (clipDialog === dlg) {
                clipDialog = null
                clipTitle = null
            }
        }
        clipDialog = dlg
        dlg.show()
    }

    private fun deleteCheckedClips() {
        val ids = clipChecked.toList()
        if (ids.isEmpty()) {
            toast("先勾选要删的素材")
            return
        }
        AlertDialog.Builder(this)
            .setTitle("删除 ${ids.size} 个素材")
            .setMessage("成品和「再次剪辑」用的原始片都会一起删掉，不能恢复。")
            .setPositiveButton("删除") { _, _ ->
                clipSelecting = false
                thread {
                    val r = apiCall("clips_remove_many", ids)
                    ui {
                        clipChecked.clear()
                        val n = r.optInt("removed")
                        val fails = r.optJSONArray("failed")
                        val failN = fails?.length() ?: 0
                        when {
                            n > 0 && failN == 0 -> toast("已删除 $n 个")
                            n > 0 -> toast("已删除 $n 个；$failN 个文件没删掉，仍在列表里")
                            else -> toast(r.optString("error", "删除失败"))
                        }
                        // 删不掉的要说出原因：留在列表里还能重试，别让人反复猜
                        if (fails != null && failN > 0) {
                            val sb = StringBuilder()
                            for (i in 0 until failN) {
                                val o = fails.optJSONObject(i) ?: continue
                                sb.append("· ").append(o.optString("name", o.optString("id")))
                                    .append("：").append(o.optString("error")).append("\n")
                            }
                            AlertDialog.Builder(this@MainActivity)
                                .setTitle("有 $failN 个没删掉")
                                .setMessage(sb.toString().trim())
                                .setPositiveButton("知道了", null)
                                .show()
                        }
                        showClips()
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    /** 一行：56dp 缩略图 + 两行文字。 */
    private fun clipRow(o: JSONObject): View {
        val id = o.optString("id", "")
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

        if (clipSelecting) {
            val cb = CheckBox(this).apply {
                isChecked = clipChecked.contains(id)
                setOnCheckedChangeListener { _, on ->
                    if (on) clipChecked.add(id) else clipChecked.remove(id)
                    clipTitle?.text = if (clipChecked.isEmpty())
                        "素材（${clipArray.length()}）" else "已选 ${clipChecked.size} 个"
                }
            }
            row.addView(cb, LinearLayout.LayoutParams(dp(40), dp(40)))
        }

        val thumb = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            // 圆角靠 outline 裁：给个圆角 drawable 当背景，再 clipToOutline
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(8).toFloat()
                setColor(0xFF1A2435.toInt())
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
            setTextColor(0xFFE8F0FB.toInt())
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
                else -> 0xFF7C8FA8.toInt()
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
            if (clipSelecting) {
                val on = !clipChecked.contains(id)
                if (on) clipChecked.add(id) else clipChecked.remove(id)
                // 只同步这一行，不重建列表 —— 重建会把滚动位置打回顶部，
                // 勾到第 10 条时特别难受。
                (row.getChildAt(0) as? CheckBox)?.isChecked = on
                clipTitle?.text = if (clipChecked.isEmpty())
                    "素材（${clipArray.length()}）" else "已选 ${clipChecked.size} 个"
                return@setOnClickListener
            }
            showClipActions(o)
        }

        if (exists) loadClipThumb(path, kind, dp(56), o.optInt("rotate", 0)) { bmp ->
            if (bmp != null) {
                thumb.setImageBitmap(bmp)
                thumb.scaleType = ImageView.ScaleType.CENTER_CROP
            }
        }
        return row
    }

    /**
     * 点一条素材的动作菜单。
     *
     * 以前只有「预览 / 删除」；现在要能再次剪辑和旋转：视频会留一份原始片
     * （`src`），有它才能回到未裁的原片重新选起止点，而不是在已经裁过的
     * 成品上二次裁（首段永远找不回来）。
     */
    private fun showClipActions(o: JSONObject) {
        val id = o.optString("id", "")
        val name = o.optString("name", "素材")
        val kind = o.optString("kind", "video")
        val src = o.optString("src", "")
        val canReedit = kind == "video" && src.isNotBlank() && File(src).isFile()
        val noTime = o.optLong("t_utc", 0L) <= 0L
        val acts = mutableListOf<String>()
        acts.add("预览")
        if (canReedit) acts.add("重新剪辑")
        if (kind == "video") acts.add("旋转 90°（当前 ${o.optInt("rotate", 0)}°）")
        acts.add("重新读取时间")
        if (noTime) acts.add("为什么没有拍摄时间？")
        acts.add("删除")
        AlertDialog.Builder(this)
            .setTitle(name)
            .setItems(acts.toTypedArray()) { _, which ->
                val a = acts[which]
                when {
                    a == "预览" -> previewClip(o)
                    a == "重新剪辑" -> reeditClip(o)
                    a.startsWith("旋转") -> rotateClip(o)
                    a == "重新读取时间" -> reprobeClip(o)
                    a == "为什么没有拍摄时间？" -> explainMissingTime(o)
                    else -> confirmDeleteClip(id, name)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun reprobeClip(o: JSONObject) {
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

    /** 每次 90° 循环。无损剪辑不转码，角度只记在素材上，绘制/抽帧时兑现。 */
    private fun rotateClip(o: JSONObject) {
        val id = o.optString("id", "")
        val next = (o.optInt("rotate", 0) + 90) % 360
        thread {
            val r = apiCall("clips_update", id, 0.0, 0, 0, next)
            ui {
                if (r.optBoolean("ok")) {
                    toast("已旋转到 $next°")
                    showClips()
                } else {
                    toast(r.optString("error", "旋转失败"))
                }
            }
        }
    }

    private fun reeditClip(o: JSONObject) {
        val src = File(o.optString("src", ""))
        if (!src.isFile) {
            toast("原始片已经不在了，没法重新剪辑；删掉这条重新从相册添加")
            return
        }
        val durMs = (o.optDouble("dur", 0.0) * 1000).toLong()
        if (durMs < 1500) {
            toast("这段视频太短，没法再剪")
            return
        }
        showTrimUi(src, durMs, o.optString("name", "素材"),
            o.optLong("t_utc", 0L), editId = o.optString("id"),
            userRotate = o.optInt("rotate", 0))
    }

    /** 视频自带的 tkhd 旋转（竖拍常见 90）。编码宽高要靠它换成显示宽高。 */
    private fun readRotation(f: File): Int {
        val mmr = android.media.MediaMetadataRetriever()
        return try {
            mmr.setDataSource(f.absolutePath)
            (mmr.extractMetadata(
                android.media.MediaMetadataRetriever.METADATA_KEY_VIDEO_ROTATION)
                ?.toIntOrNull() ?: 0)
        } catch (t: Throwable) {
            0
        } finally {
            try { mmr.release() } catch (_: Throwable) {}
        }
    }

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
    /**
     * 素材预览。**按回放卡片的比例开框**，不只按原图比例 ——
     * 卡片的框是「旋转后的外接框」，预览要是用 FIT_CENTER 随便撑，
     * 同一段素材在列表里和在成片里看起来就是两个形状。
     */
    private fun previewClip(o: JSONObject) {
        val path = o.optString("path", "")
        val kind = o.optString("kind", "video")
        val name = o.optString("name", "素材")
        val rot = o.optInt("rotate", 0)
        val w = o.optInt("w", 0)
        val h = o.optInt("h", 0)
        val swap = rot == 90 || rot == 270
        val visW = if (swap) h else w
        val visH = if (swap) w else h

        val dm = resources.displayMetrics
        val maxW = (dm.widthPixels * 0.92f).toInt()
        val maxH = (dm.heightPixels * 0.62f).toInt()
        var fw = maxW
        var fh = if (visW > 0 && visH > 0) (maxW.toFloat() * visH / visW).toInt()
                 else (maxW * 9 / 16)
        if (fh > maxH) {
            fh = maxH
            fw = if (visW > 0 && visH > 0) (maxH.toFloat() * visW / visH).toInt() else maxH
        }

        val iv = ImageView(this).apply {
            // 框已经是正确的比例，CENTER_CROP 在这里等于「刚好铺满、不裁」
            scaleType = ImageView.ScaleType.CENTER_CROP
            // 跟卡片一样是圆角深色底板，预览与成片观感一致
            background = android.graphics.drawable.GradientDrawable().apply {
                cornerRadius = dp(14).toFloat()
                setColor(0xFF12161B.toInt())
            }
            clipToOutline = true
        }
        // 用 FrameLayout 兜住尺寸：直接把带 LinearLayout.LayoutParams 的 View
        // 塞进 AlertDialog 会在 measure 时因为父容器不是 LinearLayout 而崩。
        val box = FrameLayout(this).apply {
            addView(iv, FrameLayout.LayoutParams(fw, fh))
        }
        val dlg = AlertDialog.Builder(this)
            .setTitle(name)
            .setView(box)
            .setNegativeButton("关闭", null)
            .create()
        dlg.show()
        dlg.window?.setLayout(fw + dp(32), WRAP_CONTENT)
        loadClipThumb(path, kind, maxOf(fw, fh), rot) { bmp ->
            if (bmp != null) iv.setImageBitmap(bmp)
        }
    }

    /** 取缩略图：图片降采样解码，视频抓第一帧；按素材的显示旋转摆正。绝不在主线程做。 */
    private fun loadClipThumb(path: String, kind: String, maxPx: Int,
                              rotate: Int = 0, done: (Bitmap?) -> Unit) {
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
            // 旋转只记在素材上、不转码，所以缩略图必须自己摆正 ——
            // 否则点了「旋转 90°」列表里毫无变化，看着像按钮没生效。
            val shown = bmp?.let { upright(it, rotate) }
            ui { done(shown) }
        }
    }

    /** 按角度把位图摆正（顺时针）。0° 原样返回，不做多余拷贝。 */
    private fun upright(src: Bitmap, rotate: Int): Bitmap {
        val d = ((rotate % 360) + 360) % 360
        if (d == 0) return src
        return try {
            val m = android.graphics.Matrix().apply { postRotate(d.toFloat()) }
            val out = Bitmap.createBitmap(src, 0, 0, src.width, src.height, m, true)
            if (out !== src) src.recycle()
            out
        } catch (t: Throwable) {
            Log.w(TAG, "upright bitmap", t)
            src
        }
    }

    /** 相册里图片和视频都能选：图片直接入库，视频走裁剪。支持批量选择。 */
    private fun pickMedia() {
        try {
            startActivityForResult(
                Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                    putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("video/*", "image/*"))
                    // 图片可以一次全入库；视频排队逐个裁剪（每段都要人确认起止点）
                    putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                },
                REQ_VIDEO,
            )
        } catch (t: Throwable) {
            toast("打不开相册：${t.message}")
        }
    }

    /** 批量选中的视频队列：一段剪完自动弹下一段。 */
    private val pendingVideos = ArrayDeque<Uri>()

    private fun nextPendingVideo() {
        val u = pendingVideos.removeFirstOrNull()
        if (u == null) {
            showClips()
            return
        }
        trimDialog(u)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 7301) {
            val callback = data?.getStringExtra("callback")
            if (resultCode == RESULT_OK && !callback.isNullOrBlank()) {
                finishHwLogin(callback)
            } else {
                thread { apiCall("hw_login_cancel") }
                toast("已取消华为登录")
            }
            return
        }
        if (requestCode == 7302) {
            val callback = data?.getStringExtra("callback")
            if (resultCode == RESULT_OK && !callback.isNullOrBlank()) {
                finishCorosMcpAuth(callback)
            } else {
                toast("已取消高驰授权")
                // 登录流程里跳过授权时，活动数据还没同步，不能一起丢掉
                doSync()
            }
            return
        }
        if (requestCode == REQ_MEAL_PICK || requestCode == REQ_MEAL_SHOT) {
            if (resultCode == RESULT_OK) {
                val uri = if (requestCode == REQ_MEAL_SHOT) mealPhotoUri else data?.data
                if (uri != null) doMealEstimate(uri) else toast("没拿到照片")
            }
            return
        }
        if (requestCode == REQ_BUNDLE && resultCode == android.app.Activity.RESULT_OK) {
            data?.data?.let { askImportBundle(it) }
            return
        }
        if (requestCode == REQ_GPX) {
            if (resultCode == android.app.Activity.RESULT_OK) {
                data?.data?.let { importGpx(it) }
            }
            return
        }
        if (requestCode == REQ_FILE_CHOOSER) {
            val cb = pendingFileCallback
            pendingFileCallback = null
            cb?.onReceiveValue(
                if (resultCode == RESULT_OK && data?.data != null) arrayOf(data.data!!)
                else null)
            return
        }
        if (requestCode == REQ_ATTACH) {
            if (resultCode == android.app.Activity.RESULT_OK) {
                data?.data?.let { uploadAttachment(it) }
            }
            return
        }
        if (requestCode == REQ_VIDEO && resultCode == android.app.Activity.RESULT_OK) {
            val got = data ?: return
            val uris = mutableListOf<Uri>()
            val cd = got.clipData
            if (cd != null && cd.itemCount > 0) {
                for (i in 0 until cd.itemCount) cd.getItemAt(i)?.uri?.let { uris.add(it) }
            } else {
                got.data?.let { uris.add(it) }
            }
            if (uris.isEmpty()) return
            val vids = mutableListOf<Uri>()
            val imgs = mutableListOf<Pair<Uri, String>>()
            for (u in uris) {
                val mime = try {
                    contentResolver.getType(u) ?: ""
                } catch (t: Throwable) {
                    ""
                }
                if (mime.startsWith("image/")) imgs.add(u to mime) else vids.add(u)
            }
            // 多张图片：全部加完再刷新列表，别每张都重建一次对话框
            val left = java.util.concurrent.atomic.AtomicInteger(imgs.size)
            for ((u, m) in imgs) addImage(u, m, left)
            if (vids.isNotEmpty()) {
                pendingVideos.clear()
                pendingVideos.addAll(vids)
                nextPendingVideo()
            }
        }
    }

    /**
     * 图片素材：拷进 data/media 登记即可，不需要裁剪。
     *
     * 常见格式**原样**拷，保留 EXIF 的拍摄时间与坐标——回放靠它把素材放到正确位置。
     * HEIC 之类浏览器解不了的，解码重编成 JPEG（会丢 EXIF，位置由后端均匀铺开兜底）。
     */
    private fun addImage(src: Uri, mime: String,
                         left: java.util.concurrent.atomic.AtomicInteger? = null) {
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
                val done = left == null || left.decrementAndGet() == 0
                if (add.optBoolean("ok")) {
                    toast("已加入素材（图片）")
                } else {
                    toast(add.optString("error", "登记失败"))
                }
                // 批量时等最后一张再加完刷新，否则每张都重建一次列表对话框
                if (done) showClips()
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
                ui {
                    busy(false)
                    toast("读取视频失败：${t.message}")
                    if (pendingVideos.isNotEmpty()) nextPendingVideo() else showClips()
                }
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
                    if (pendingVideos.isNotEmpty()) nextPendingVideo() else showClips()
                } else {
                    showTrimUi(tmp, durMs, displayName, takenMs)
                }
            }
        }
    }

    private fun showTrimUi(src: File, durMs: Long, displayName: String, takenMs: Long,
                           editId: String? = null, userRotate: Int = 0) {
        // 预览 + 关键帧胶片条：光有两个滑块根本不知道第几秒拍的是什么，
        // 也没法知道落刀会不会被吸附到别处。见 ClipTrimDialog。
        ClipTrimDialog.show(this, src, durMs, userRotate,
            onPick = { s, e, rot ->
                saveClip(src, s, e, displayName, takenMs, editId, rot)
            },
            onCancel = {
                // 再次剪辑的 src 是长期保留的原始片，取消时绝不能删
                if (editId == null) src.delete()
                if (pendingVideos.isNotEmpty()) nextPendingVideo() else showClips()
            })
    }

    private fun saveClip(src: File, startMs: Long, endMs: Long,
                         displayName: String, takenMs: Long,
                         editId: String? = null, userRotate: Int = 0) {
        busy(true)
        subtitle.text = "剪辑中…"
        thread {
            val outDir = File(filesDir, "data/media").apply { mkdirs() }
            val dst = File(outDir, "clip-" + System.currentTimeMillis() + ".mp4")
            val r = ClipTrimmer.trim(src, dst, startMs, endMs)
            if (!r.ok) {
                if (editId == null) src.delete()
                ui {
                    busy(false); updateSubtitle()
                    toast("剪辑失败：${r.message}")
                    if (pendingVideos.isNotEmpty()) nextPendingVideo() else showClips()
                }
                return@thread
            }
            // 编码宽高是「横向」的：竖拍视频 tkhd 带 rotation=90，播放器会自己转，
            // 但回放卡片是按登记尺寸布局的 —— 不换过来就会被压成横屏。
            // MediaMuxer 偶尔会丢掉 rotation，那就退回读源片。
            val rot = readRotation(dst).takeIf { it != 0 } ?: readRotation(src)
            val dw = if (rot == 90 || rot == 270) r.height else r.width
            val dh = if (rot == 90 || rot == 270) r.width else r.height

            val call: JSONObject
            if (editId != null) {
                // 覆盖原成品：先把新裁的挪到它现在的路径上，再回写记录
                val oldPath = (0 until clipArray.length())
                    .mapNotNull { clipArray.optJSONObject(it) }
                    .firstOrNull { it.optString("id") == editId }
                    ?.optString("path") ?: ""
                val target = if (oldPath.isNotBlank()) File(oldPath) else dst
                if (target.absolutePath != dst.absolutePath) {
                    try {
                        target.delete()
                        if (!dst.renameTo(target)) {
                            dst.copyTo(target, overwrite = true)
                            dst.delete()
                        }
                    } catch (t: Throwable) {
                        Log.w(TAG, "replace clip", t)
                    }
                }
                call = apiCall("clips_update", editId, r.durMs / 1000.0,
                    dw, dh, userRotate)
            } else {
                // 留一份原始片，供「重新剪辑」回到未裁的版本
                var keep = ""
                try {
                    val k = File(outDir, "src-" + System.currentTimeMillis() + ".mp4")
                    src.copyTo(k, overwrite = true)
                    if (k.length() > 0) keep = k.absolutePath
                } catch (t: Throwable) {
                    Log.w(TAG, "keep src", t)
                }
                // 关键：把**源文件**和**相册拍摄时间**都传过去。MediaMuxer 重封装会丢
                // creation_time；不少手机把拍摄时间只写在 MediaStore / 解码器日期标签里，
                // 裁完的成品和拷进 cache 的源片都读不出来，只能在选片这一刻取走。
                call = apiCall("clips_add", dst.absolutePath, displayName,
                    r.durMs / 1000.0, dw, dh, "video", src.absolutePath, takenMs,
                    userRotate, keep)
            }
            if (editId == null) src.delete()   // 临时 cache 可以删，长期保留片不能
            ui {
                busy(false)
                updateSubtitle()
                if (call.optBoolean("ok")) {
                    // 实际落刀点可能比选定的早（吸附到关键帧），说出来免得以为剪错了
                    val drift = startMs - r.startMs
                    val extra = if (drift > 200) "，实际从 ${r.startMs / 1000.0} 秒开始" else ""
                    val noTime = if (editId == null && call.optLong("t_utc", 0L) <= 0L)
                        "，⚠ 没读到拍摄时间" else ""
                    toast((if (editId != null) "已更新素材" else "已加入素材") +
                        "（${r.durMs / 1000} 秒$extra）$noTime")
                } else {
                    toast(call.optString("error", "登记失败"))
                }
                if (pendingVideos.isNotEmpty()) nextPendingVideo() else showClips()
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

    private fun showBodySourcePicker() {
        thread {
            val s = apiCall("body_sources")
            ui {
                if (!s.optBoolean("ok")) {
                    toast("读取体重来源失败：${s.optString("error")}")
                    return@ui
                }
                val ids = listOf("mi", "huawei")
                val preferred = s.optString("preferred", "mi").let {
                    if (it in ids) it else "mi"
                }
                val names = arrayOf("小米", "华为")
                AlertDialog.Builder(this)
                    .setTitle("首选体重来源")
                    .setSingleChoiceItems(names, ids.indexOf(preferred)) { dialog, which ->
                        dialog.dismiss()
                        thread {
                            val r = apiCall("body_set_source", ids[which])
                            ui {
                                if (r.optBoolean("ok")) {
                                    toast("已更新体重来源；无记录的日期自动使用其他来源")
                                    web.reload()
                                } else toast("设置失败：${r.optString("error")}")
                            }
                        }
                    }
                    // 小米/华为的登录并进来了：三个体重行合成一行后，
                    // 「登录哪个秤」放进同一个选择器，不必再回列表找入口
                    .setNeutralButton("登录账号") { _, _ ->
                        AlertDialog.Builder(this)
                            .setTitle("登录体重秤账号")
                            .setItems(arrayOf("小米运动健康", "华为运动健康")) { _, w ->
                                if (w == 0) showMiPanel() else showHwPanel()
                            }
                            .setNegativeButton("取消", null)
                            .show()
                    }
                    .setNegativeButton("取消", null)
                    .show()
            }
        }
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
                                "选择小米为首选体重来源后，日常下拉刷新也会自动同步。"
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

    /** 华为网页登录授权后自动换健康会话，随后使用共用的体重同步流程。 */
    private fun showHwPanel() {
        thread {
            val s = apiCall("hw_status")
            ui {
                if (!s.optBoolean("ok")) {
                    toast("状态读取失败：${s.optString("error")}")
                    return@ui
                }
                val logged = s.optBoolean("logged_in")
                val uid = s.optString("open_id")
                val refresh = if (s.optBoolean("has_refresh")) "可自动续期" else "过期需重新登录"
                val dlg = AlertDialog.Builder(this)
                    .setTitle("华为体重")
                    .setMessage(
                        if (logged)
                            "已登录华为账号（$uid），$refresh。\n\n" +
                                "「同步体重」拉取运动健康的体成分记录并回填看板；\n" +
                                "选择华为为首选体重来源后，日常下拉刷新也会自动同步。"
                        else
                            "还没登录华为账号。\n\n" +
                                "登录绑定体重秤的华为账号，在华为页面完成验证和授权，" +
                                "之后自动同步运动健康云端的体重。\n\n" +
                                "当前借助运动健康的授权入口，页面可能显示「运动健康」。" +
                                "密码仅提交给华为；同步会话只存本机。"
                    )
                    .setNegativeButton("关闭", null)
                if (logged) {
                    dlg.setPositiveButton("同步体重") { _, _ -> doHwSync() }
                    dlg.setNeutralButton("体检") { _, _ -> doHwProbe() }
                    dlg.setNegativeButton("退出登录") { _, _ -> doHwLogout() }
                } else {
                    dlg.setPositiveButton("登录华为账号") { _, _ -> askHwLogin() }
                }
                dlg.show()
            }
        }
    }

    private fun askHwLogin() {
        busy(true)
        thread {
            val r = apiCall("hw_login_start")
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    startActivityForResult(
                        Intent(this, HuaweiLoginActivity::class.java)
                            .putExtra("url", r.optString("url"))
                            .putExtra("redirect_uri", r.optString("redirect_uri")),
                        7301
                    )
                } else toast("打开华为登录失败：${r.optString("error")}")
            }
        }
    }

    private fun finishHwLogin(callback: String) {
        busy(true)
        thread {
            val r = apiCall("hw_login_finish", callback)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    toast("华为账号登录成功")
                    doHwSync()
                } else {
                    AlertDialog.Builder(this)
                        .setTitle("华为登录未完成")
                        .setMessage(r.optString("error"))
                        .setPositiveButton("重新登录") { _, _ -> askHwLogin() }
                        .setNegativeButton("关闭", null)
                        .show()
                }
            }
        }
    }

    private fun askHwImport() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 24, 56, 0)
        }
        val eJson = EditText(this).apply {
            hint = "粘贴会话 JSON：{\"accessToken\":\"…\",\"refreshToken\":\"…\",\"uid\":\"…\"}"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 3
        }
        box.addView(eJson)
        AlertDialog.Builder(this)
            .setTitle("导入华为会话")
            .setMessage("会话 JSON 只存本机 data/huawei_auth.json；" +
                "带 refreshToken 的会话可自动续期。")
            .setView(box)
            .setNegativeButton("取消", null)
            .setPositiveButton("导入") { _, _ ->
                val raw = eJson.text.toString().trim()
                if (raw.isBlank()) toast("要先粘贴会话 JSON")
                else doHwImport(raw)
            }
            .show()
    }

    private fun doHwImport(payload: String) {
        busy(true)
        thread {
            val r = apiCall("hw_import", payload)
            ui {
                busy(false)
                if (r.optBoolean("ok")) {
                    toast("已导入华为会话（${r.optString("open_id")}）")
                    doHwSync()
                } else toast("导入失败：${r.optString("error")}")
            }
        }
    }

    private fun doHwSync() = doScaleSync("huawei")

    private fun doHwProbe() {
        busy(true)
        thread {
            val r = apiCall("hw_probe")
            ui {
                busy(false)
                if (!r.optBoolean("ok")) {
                    toast("体检失败：${r.optString("error")}")
                    return@ui
                }
                val gates = r.optJSONObject("gates")
                val names = mapOf("account" to "账号登录",
                                  "weight" to "体重读取", "renew" to "会话续期")
                val lines = names.mapNotNull { (k, label) ->
                    val g = gates?.optJSONObject(k) ?: return@mapNotNull null
                    "${if (g.optBoolean("ok")) "√" else "×"} $label　${g.optString("detail")}"
                }
                AlertDialog.Builder(this)
                    .setTitle(if (r.optBoolean("all_ok")) "体检通过" else "体检未通过")
                    .setMessage(lines.joinToString("\n").ifBlank { "没有返回体检明细" })
                    .setPositiveButton("关闭", null)
                    .show()
            }
        }
    }

    private fun doHwLogout() {
        thread {
            val r = apiCall("hw_logout")
            ui {
                toast(if (r.optBoolean("ok")) "已删除华为会话（云端数据不受影响）"
                      else "退出失败：${r.optString("error")}")
                web.reload()
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
        addShowPassword(box, ePwd)
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
    private fun doMiSync() = doScaleSync("mi")

    private fun doScaleSync(source: String) {
        val huawei = source == "huawei"
        val brand = if (huawei) "华为" else "小米"
        val healthApp = if (huawei) "华为运动健康" else "小米运动健康"
        busy(true)
        ui { subtitle.text = "正在拉取${brand}体重…" }
        thread {
            val r = apiCall(if (huawei) "hw_sync" else "mi_sync", 60)
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
                        "云端这段时间没有新称重记录——上秤称一次，打开「${healthApp}」让它上传后再试"
                    toast(msg)
                    thread {
                        apiCall("dashboard")
                        ui { reload() }
                    }
                } else if (r.optBoolean("need_login")) {
                    toast("${brand}登录已失效，请重新授权")
                    if (huawei) askHwLogin() else askMiLogin()
                } else {
                    toast("体重同步失败：${r.optString("error")}")
                }
            }
        }
    }

    // ------------------------------------------------------------ 诊断
    /** 当前 App 版本名（1.0.4）。不依赖 BuildConfig（AGP 8 默认不生成）。 */
    private fun appVersion(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: ""
    } catch (e: Exception) {
        ""
    }

    // ------------------------------------------------------------ 检查更新
    /**
     * 查 Gitee 上的最新 Release。
     *
     * 分发只有 Gitee Release 一个渠道（没有应用商店），所以「有没有新版本」
     * 直接问 Gitee 的公开接口，不需要自建后端、不需要推送。
     * 静默检查每天最多一次（gc_prefs 里记时间戳），用户点「稍后」会把该版本
     * 记成已跳过，下次换版本才再提；手动检查不受节流。
     * 下载走浏览器：APK 是正式签名，Android 覆盖安装会校验签名一致，
     * 不会出现「装上一个同包名的野包」。
     */
    private fun checkUpdate(silent: Boolean) {
        if (port == 0) return
        val prefs = getSharedPreferences("gc_prefs", MODE_PRIVATE)
        val ver = appVersion()
        if (silent) {
            val last = prefs.getLong("update_checked_at", 0L)
            val lastVer = prefs.getString("update_checked_ver", "") ?: ""
            // 升到新版本必须立刻查一次：否则「刚装的新包 + 昨天查过」会一直静默，
            // 用户就永远等不到提示（1.0.3 没弹窗正是这个原因）。
            val sameVer = lastVer == ver
            if (sameVer && last > 0
                && System.currentTimeMillis() - last < 24L * 3600 * 1000) {
                Log.i(TAG, "checkUpdate skip：24h 内已查过（$lastVer）")
                return
            }
        }
        thread {
            val r = apiCall("check_update")
            ui {
                if (!r.optBoolean("ok")) {
                    // 失败不记时间戳：网络抖一下不该把下次检查堵 24 小时
                    Log.i(TAG, "checkUpdate 失败：" + r.optString("error"))
                    if (!silent) toast("检查更新失败，稍后再试（不影响使用）")
                    return@ui
                }
                prefs.edit()
                    .putLong("update_checked_at", System.currentTimeMillis())
                    .putString("update_checked_ver", ver)
                    .apply()
                Log.i(TAG, "checkUpdate 结果：latest=" + r.optString("latest")
                    + " newer=" + r.optBoolean("newer") + " current=$ver")
                val tag = r.optString("latest", "")
                if (!r.optBoolean("newer")) {
                    if (!silent) toast("已是最新版本 " + (r.optString("current") ?: ""))
                    return@ui
                }
                // 用户点过「稍后」的版本不再打扰，等下一个版本
                if (silent && prefs.getString("update_skipped", "") == tag) return@ui
                val notes = r.optString("notes", "")
                val tv = TextView(this).apply {
                    text = notes.ifBlank { "本次更新没有写说明，点「去下载」看发布页。" }
                    textSize = 13f
                    setPadding(dp(20), dp(8), dp(20), dp(8))
                }
                val sv = ScrollView(this).apply { addView(tv) }
                AlertDialog.Builder(this)
                    .setTitle("发现新版本 " + tag)
                    .setView(sv)
                    .setPositiveButton("去下载") { _, _ ->
                        val url = r.optString("url", "")
                        if (url.isBlank()) {
                            toast("没有拿到下载地址，请到 Gitee 发布页手动下载")
                        } else {
                            try {
                                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                            } catch (e: Exception) {
                                toast("打不开浏览器：$url")
                            }
                        }
                    }
                    .setNegativeButton("稍后") { _, _ ->
                        prefs.edit().putString("update_skipped", tag).apply()
                    }
                    .show()
            }
        }
    }

    /**
     * 最近几条异常。用可选中复制的 TextView 而不是 WebView 页面：
     * 用户要的是「把这段发给你」，多一次跳转就多一次放弃。
     */
    private fun showDiag() {
        busy(true)
        thread {
            val r = apiCall("diag_text", 300)
            ui {
                busy(false)
                val txt = r.optString("text", "（读不出来）")
                val tv = TextView(this).apply {
                    text = txt
                    setTextIsSelectable(true)
                    textSize = 11.5f
                    typeface = android.graphics.Typeface.MONOSPACE
                    setPadding(dp(16), dp(12), dp(16), dp(12))
                }
                val sv = ScrollView(this).apply { addView(tv) }
                AlertDialog.Builder(this)
                    .setTitle("诊断日志")
                    .setView(sv)
                    .setPositiveButton("关闭", null)
                    .setNeutralButton("复制") { _, _ ->
                        val cm = getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                            as android.content.ClipboardManager
                        cm.setPrimaryClip(android.content.ClipData.newPlainText("diag", txt))
                        toast("已复制，直接发给开发者即可")
                    }
                    .show()
            }
        }
    }

    // ------------------------------------------------------------ 数据搬迁
    /**
     * 导出数据包。
     *
     * App 是 allowBackup=false，卸载/换机就等于历史清零，所以必须给用户一条
     * 自己把数据搬走的通路。zip 先由 Python 侧用 sqlite backup API 生成，
     * 这里再复制到公共「下载/RunnerCoach」，用户既能自己在文件管理器里找到，
     * 也能一口气分享出去。
     */
    private fun doExportData() {
        if (port == 0) {
            toast("还在启动，稍等一下")
            return
        }
        busy(true)
        thread {
            val r = apiCall("db_export")
            ui {
                busy(false)
                if (!r.optBoolean("ok")) {
                    toast("导出失败：${r.optString("error")}")
                    return@ui
                }
                val src = File(r.optString("path"))
                val name = r.optString("name", "runnercoach-data.zip")
                val uri = try {
                    publishToDownloads(src, name)
                } catch (t: Throwable) {
                    Log.e(TAG, "publish bundle failed", t)
                    null
                }
                if (uri == null) {
                    toast("导出失败：写不进下载目录")
                    return@ui
                }
                val c = r.optJSONObject("counts")
                val days = c?.optInt("daily", 0) ?: 0
                val acts = c?.optInt("activities", 0) ?: 0
                val mb = String.format(Locale.US, "%.1f", src.length() / 1048576.0)
                // 「没带什么」由 Python 侧给，避免两边各写一份、改一处漏一处
                val excl = r.optJSONArray("excluded")?.let { a ->
                    (0 until a.length()).joinToString("\n") { "· " + a.optString(it) }
                } ?: ""
                AlertDialog.Builder(this)
                    .setTitle("已导出")
                    .setMessage(
                        "$days 天日汇总 / $acts 条活动，共 $mb MB。\n\n" +
                            "已存到「下载/RunnerCoach/$name」，也可以直接分享出去。\n\n" +
                            (if (excl.isEmpty()) "" else
                                "为了安全，包里不含：\n$excl\n\n换机后重新登录 / 重填即可。")
                    )
                    .setPositiveButton("分享") { _, _ -> shareZip(uri) }
                    .setNegativeButton("知道了", null)
                    .show()
            }
        }
    }

    /** 写进公共下载目录，返回可分享的 content:// uri。 */
    private fun publishToDownloads(src: File, name: String): Uri? {
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, name)
            put(MediaStore.Downloads.MIME_TYPE, "application/zip")
            put(MediaStore.Downloads.RELATIVE_PATH,
                Environment.DIRECTORY_DOWNLOADS + "/RunnerCoach")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return null
        try {
            contentResolver.openOutputStream(uri)?.use { out ->
                src.inputStream().use { it.copyTo(out) }
            } ?: return null
        } catch (t: Throwable) {
            contentResolver.delete(uri, null, null)
            throw t
        }
        values.clear()
        values.put(MediaStore.Downloads.IS_PENDING, 0)
        contentResolver.update(uri, values, null, null)
        return uri
    }

    private fun shareZip(uri: Uri) {
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "application/zip"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(i, "分享数据包"))
    }

    private fun doImportData() {
        try {
            startActivityForResult(
                Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    type = "*/*"
                    // 有的文件管理器把 zip 报成 octet-stream，两个都收；
                    // 真正的校验在 Python 侧（看 manifest.json），不靠后缀。
                    putExtra(Intent.EXTRA_MIME_TYPES,
                        arrayOf("application/zip", "application/octet-stream"))
                },
                REQ_BUNDLE,
            )
        } catch (t: Throwable) {
            toast("打不开文件选择器：${t.message}")
        }
    }

    private fun askImportBundle(uri: Uri) {
        AlertDialog.Builder(this)
            .setTitle("导入数据？")
            .setMessage(
                "会用数据包里的库覆盖当前数据。\n" +
                    "覆盖前会自动备份一份 garmin.db.bak-时间戳，导错了还能换回来。\n\n" +
                    "账号需要重新登录，照片素材需要重新添加（包里不含这些）。"
            )
            .setPositiveButton("导入") { _, _ -> doImportBundle(uri) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun doImportBundle(uri: Uri) {
        busy(true)
        thread {
            val tmp = File(cacheDir, "import-bundle.zip")
            val copied = try {
                contentResolver.openInputStream(uri)?.use { input ->
                    tmp.outputStream().use { input.copyTo(it) }
                }
                tmp.isFile && tmp.length() > 0
            } catch (t: Throwable) {
                Log.e(TAG, "copy bundle failed", t)
                false
            }
            if (!copied) {
                ui { busy(false); toast("读不出这个文件") }
                return@thread
            }
            val r = apiCall("db_import", tmp.absolutePath)
            tmp.delete()
            ui {
                busy(false)
                if (!r.optBoolean("ok")) {
                    toast("导入失败：${r.optString("error")}")
                    return@ui
                }
                val c = r.optJSONObject("counts")
                val days = c?.optInt("daily", 0) ?: 0
                val acts = c?.optInt("activities", 0) ?: 0
                val bak = r.optString("backup")
                AlertDialog.Builder(this)
                    .setTitle("导入完成")
                    .setMessage(
                        "$days 天日汇总 / $acts 条活动。\n" +
                            (if (bak.isEmpty()) "" else "\n原库已备份到：\n${File(bak).name}")
                    )
                    .setPositiveButton("好") { _, _ ->
                        goTab(0)
                        reload()
                    }
                    .show()
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
                    "「隐藏轨迹」不显示地图。\n\n" +
                    "回放视频在后台生成（通常一两分钟），可以先切到别的页面，"
                    + "进度看「我的 → 产物文件」。"
            )
            .setSingleChoiceItems(PRIVACY_LABELS, checked) { _, which -> checked = which }
            .setNeutralButton("回放视频（后台）") { _, _ ->
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
            // 真机上排查底图缩放：k 是底图与轨迹共用的视口缩放，
            // ratio 是变换前轨迹外框占画幅的比例（过大 = 旧版会顶到边缘）。
            Log.i(TAG, "motion view=" + r.optJSONObject("view"))
            // 原生预抽素材帧：必须在起 WebView 之前做完 —— 这时还没有监督
            // 线程，不会撞上「连续 5 分钟没有新帧」的判死。抽不到帧的段
            // n=0，页面退回静态封面，绝不再在 WebView 里 drawImage(<video>)。
            prepareClipFrames(r)
            ui {
                startMotionView(
                    r.optString("run"), r.optInt("frames"), r.optInt("fps", 24),
                    r.optString("html"), r.optString("name", "跑步回放"),
                )
            }
        }
    }

    /** 把每段视频按 12fps 抽成缩略 JPEG，再写 `_clips.json` 给页面按 ci 取图。 */
    private fun prepareClipFrames(r: org.json.JSONObject) {
        val arr = r.optJSONArray("clips") ?: return
        val dir = r.optString("dir")
        if (arr.length() == 0 || dir.isEmpty()) return
        val root = java.io.File(dir)
        val index = org.json.JSONObject()
        for (i in 0 until arr.length()) {
            val c = arr.optJSONObject(i) ?: continue
            val ci = c.optInt("ci", i)
            ui { subtitle.text = "准备素材画面 ${i + 1}/${arr.length()}" }
            val t0 = System.currentTimeMillis()
            // 12fps / 720px / 96 格：8fps 时卡片画面明显一顿一顿，成片 24fps
            // 只是把它按住 3 帧。提到 12fps 顺很多，分辨率让到 720 换回体积
            // （12/8 × (720/840)² ≈ 1.10，抽帧总量基本不变）。
            val info = MotionClips.extract(
                c.optString("file"), java.io.File(root, "c$ci"), 12, 96, 720,
                c.optInt("rotate", 0))
            try {
                index.put(ci.toString(), org.json.JSONObject()
                    .put("n", info.n).put("fps", info.fps)
                    .put("w", info.w).put("h", info.h))
            } catch (_: Throwable) {}
            Log.i(TAG, "motion clip ${c.optString("name")}: ${info.n} 帧 @ " +
                "${String.format(Locale.US, "%.1f", info.fps)}fps " +
                "${info.w}x${info.h}, " +
                "${(System.currentTimeMillis() - t0) / 1000}s")
        }
        try {
            java.io.File(root, "_clips.json").writeText(index.toString(), Charsets.UTF_8)
        } catch (t: Throwable) {
            Log.w(TAG, "写 _clips.json 失败: ${t.message}")
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
            if (show >= 0) {
                ui {
                    subtitle.text = "生成回放 $show%"
                    busy(true, show)   // 顶部细线跟着真实进度走
                }
                // 同时落一份到磁盘：「我的 → 产物文件」靠它显示进度条，
                // 用户切走、锁屏回来也还能看到「还在生成」。
                writeJob(run, title, show, "生成回放")
            }
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
            var loggedPre = ""
            while (!encoded.get() && !cancel.get()) {
                Thread.sleep(700)
                val st = apiCall("motion_status", run)
                val n = st.optInt("frames")
                // 每段视频的预抽帧结果：真机上能确认走的是新路径、
                // 抽到多少格真正有画面（lit=0 就说明视频解码有问题）
                st.optJSONObject("diag")?.optJSONObject("preload")?.let { p ->
                    val key = "${p.optString("name")}/${p.optInt("n")}/${p.optInt("lit")}"
                    if (key != loggedPre) {
                        loggedPre = key
                        Log.i(TAG, "motion preload: ${p.optString("name")} " +
                            "${p.optInt("lit")}/${p.optInt("n")} frames " +
                            "@${p.optDouble("fps")}fps " +
                            "${p.optInt("w")}x${p.optInt("h")}")
                    }
                }
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
                clearJob(run)
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
            clearJob(run)
            // 统一收尾：成功、失败、诊断取消还是超时，都得把进度线收掉
            ui {
                busy(false)
                updateSubtitle()
                // 生成完成的提示由 encodeMotion 给（带耗时与编码方式）；
                // 这里只负责一件事：人在产物页时把列表刷出来。
                if (ok && web.url?.contains("/files") == true) reload()
            }
        }
    }

    /**
     * 把回放生成进度写到 `output/_progress/<run>.json`。
     *
     * 为什么落盘而不是只在内存：生成要跑几分钟，用户会切 tab、锁屏、去翻别的页面。
     * 「我的 → 产物文件」每 2 秒拉一次 `/__progress`，读的就是这些文件，
     * 所以切走再回来仍然能看到「还在生成 42%」。
     */
    private fun writeJob(run: String, name: String, pct: Int, stage: String) {
        try {
            val d = File(filesDir, "output/_progress").apply { mkdirs() }
            val obj = JSONObject()
                .put("run", run)
                .put("name", name)
                .put("pct", pct)
                .put("stage", stage)
                .put("at", System.currentTimeMillis())
            File(d, "$run.json").writeText(obj.toString(), Charsets.UTF_8)
        } catch (t: Throwable) {
            Log.w(TAG, "write job progress failed", t)
        }
    }

    private fun clearJob(run: String) {
        try {
            File(File(filesDir, "output/_progress"), "$run.json").delete()
        } catch (t: Throwable) {
            Log.w(TAG, "clear job progress failed", t)
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
            toast("回放已存相册 · ${seconds}s · ${r.via} 编码$tail")
            // 不再自动弹分享面板：生成已经改成后台任务，这几分钟里用户很可能
            // 切到了别的页面，突然盖一层分享面板等于打断。要分享去相册，
            // 或到「我的 → 产物文件」找到这个 mp4。
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
            // 版本号 + 实测缩放系数只给调试包看：真机测试时一眼能确认装的是新包，
            // 也能把 ×0.xx 直接报回来对账。正式包里这是纯噪音，还显得没打磨。
            // 工程没开 buildConfig 特性（AGP 8 默认不生成），用 debuggable 标记判断。
            val debuggable =
                (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0
            if (debuggable) {
                val ver = try {
                    packageManager.getPackageInfo(packageName, 0).versionName ?: "?"
                } catch (t: Throwable) {
                    "?"
                }
                toast("$ver 出图缩放 ×" + String.format(Locale.US, "%.2f", f))
            }
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
        // 中间位图用完立刻回收：1080×1920 的 ARGB_8888 一张就是 8.3 MB，
        // 出图这一下同时存在原图 + 缩放图 + 成品，低端机容易顶到堆上限。
        if (scaled !== src) scaled.recycle()
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
    /** 系统文件选择器：规程 PDF、报名截图、补充说明都能选。 */
    private fun pickAttachment(slug: String?) {
        pendingAttachSlug = slug
        val i = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf(
                "application/pdf", "image/*", "text/*",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        }
        try {
            startActivityForResult(i, REQ_ATTACH)
        } catch (t: Throwable) {
            toast("打不开文件选择器：" + (t.message ?: t.javaClass.simpleName))
        }
    }

    /**
     * 把选中的文件传到赛事目录（走本机回环）。
     *
     * 文件名用 ContentResolver 查到的显示名：content:// 的 Uri 末段是个随机 id，
     * 直接拿来当文件名，用户就再也认不出哪份是规程、哪份是参赛号了。
     */
    private fun uploadAttachment(uri: Uri) {
        val name = run {
            try {
                contentResolver.query(uri, null, null, null, null)?.use { c ->
                    val i = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                    if (i >= 0 && c.moveToFirst()) c.getString(i) else null
                }
            } catch (t: Throwable) {
                null
            }
        } ?: "附件"
        toast("正在上传 " + name + "…")
        thread {
            try {
                val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                    ?: throw IllegalStateException("读不到文件内容")
                if (bytes.isEmpty()) throw IllegalStateException("这个文件是空的")
                if (bytes.size > 20 * 1024 * 1024)
                    throw IllegalStateException("文件超过 20 MB")
                val p = port
                if (p <= 0) throw IllegalStateException("本地服务还没起来，稍后再试")
                val q = java.net.URLEncoder.encode(name, "UTF-8")
                val s = java.net.URLEncoder.encode(pendingAttachSlug ?: "", "UTF-8")
                val c = java.net.URL(
                    "http://127.0.0.1:$p/__event/attach?slug=$s&name=$q").openConnection()
                        as java.net.HttpURLConnection
                c.requestMethod = "POST"
                c.doOutput = true
                c.connectTimeout = 20000
                c.readTimeout = 60000
                c.setRequestProperty("Content-Type", "application/octet-stream")
                c.setFixedLengthStreamingMode(bytes.size)
                // 不自动跟随 303：目的地带着 ok= 与 at=docs，跟随之后这些就丢了，
                // 页面刷新出来看不到「已添加附件」那条绿条
                c.instanceFollowRedirects = false
                c.outputStream.use { it.write(bytes) }
                val code = c.responseCode
                val loc = c.getHeaderField("Location")
                val txt = (if (code in 200..299) c.inputStream else c.errorStream)
                    ?.bufferedReader()?.use { it.readText() } ?: ""
                c.disconnect()
                ui {
                    if (code in 200..399 && loc != null) {
                        if (this::web.isInitialized) {
                            web.loadUrl("http://127.0.0.1:$p" + loc)
                        }
                    } else if (code in 200..399) {
                        if (this::web.isInitialized) {
                            web.loadUrl("http://127.0.0.1:$p/event")
                        }
                    } else {
                        val msg = try {
                            JSONObject(txt).optString("error", "上传失败")
                        } catch (t: Throwable) {
                            "上传失败（HTTP " + code + "）"
                        }
                        AlertDialog.Builder(this).setTitle("上传失败")
                            .setMessage(msg).setPositiveButton("好", null).show()
                    }
                }
            } catch (t: Throwable) {
                ui {
                    AlertDialog.Builder(this).setTitle("上传失败")
                        .setMessage(t.message ?: t.javaClass.simpleName)
                        .setPositiveButton("好", null).show()
                }
            }
        }
    }

    /**
     * 用系统应用打开本机服务上的附件（规程 PDF、参赛号截图）。
     *
     * 为什么不在 WebView 里直接点链接：Android WebView 没有 PDF 渲染器，
     * 点了只会白屏；而 127.0.0.1 属于「同源」，shouldOverrideUrlLoading
     * 也不会触发。交给系统后，浏览器 / PDF 阅读器 / 相册都能读这个地址。
     */
    private fun openLocalFile(rel: String) {
        val p = port
        if (p <= 0) {
            toast("本地服务还没起来，稍后再试")
            return
        }
        val path = if (rel.startsWith("/")) rel else "/" + rel
        try {
            startActivity(Intent(Intent.ACTION_VIEW,
                Uri.parse("http://127.0.0.1:" + p + path)))
        } catch (t: Throwable) {
            toast("没有能打开它的应用：" + (t.message ?: t.javaClass.simpleName))
        }
    }

    // ------------------------------------------------------------ GPX 导入
    /**
     * 从 intent 里取 GPX 的 Uri。
     *
     * 支持三种入口：ACTION_VIEW（文件管理器「用其他应用打开」）、
     * ACTION_SEND（微信 / 浏览器「分享到」）、ACTION_EDIT（少数文件管理器）。
     * 不校验扩展名——有些来源只给 MIME，扩展名反而缺失；真正的内容合法性
     * 由 Python 侧的 GPX 解析器把关（解析失败会明确报错，不会写脏数据）。
     */
    private fun gpxFrom(it: Intent?): Uri? {
        if (it == null) return null
        return when (it.action) {
            Intent.ACTION_VIEW, Intent.ACTION_EDIT -> it.data
            Intent.ACTION_SEND -> {
                @Suppress("DEPRECATION")
                it.getParcelableExtra<Uri>(Intent.EXTRA_STREAM) ?: it.data
            }
            else -> null
        }
    }

    /** 系统文件选择器：只列 GPX / XML，避免在几千个文件里翻。 */
    private fun pickGpx() {
        val i = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            // 有些文件管理器把 .gpx 报成 application/octet-stream，
            // 所以 type 放宽到 */*，再用 EXTRA_MIME_TYPES 收窄候选。
            type = "*/*"
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf(
                "application/gpx+xml", "application/xml", "text/xml",
                "application/octet-stream"))
        }
        try {
            startActivityForResult(i, REQ_GPX)
        } catch (t: Throwable) {
            toast("打不开文件选择器：" + (t.message ?: t.javaClass.simpleName))
        }
    }

    /**
     * 把 GPX 交给本地服务解析。
     *
     * 读 content:// 必须走 ContentResolver（不能当路径直接读），
     * 上传走回环 HTTP：Python 侧已经有解析器，两边共用一份逻辑，
     * 不做第二套 Kotlin 解析（否则数字会和桌面端对不上）。
     */
    private fun importGpx(uri: Uri) {
        // 必须是「当前正在看的这一场」。以前不带 slug，后端只能按日期取「下一场」，
        // 结果在 105K 页面导入的轨迹落到了 43K 的目录里（用户 2026-11-12 反馈）。
        val curSlug = try {
            android.net.Uri.parse(web.url ?: "").getQueryParameter("slug") ?: ""
        } catch (t: Throwable) {
            ""
        }
        toast("正在读取 GPX…")
        thread {
            try {
                val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                    ?: throw IllegalStateException("读不到文件内容")
                if (bytes.isEmpty()) throw IllegalStateException("这个文件是空的")
                if (bytes.size > 40 * 1024 * 1024)
                    throw IllegalStateException("文件超过 40 MB，不像 GPX")
                val p = port
                if (p <= 0) throw IllegalStateException("本地服务还没起来，稍后再试")
                val q = if (curSlug.isNotBlank())
                    "?slug=" + java.net.URLEncoder.encode(curSlug, "UTF-8") else ""
                val c = java.net.URL("http://127.0.0.1:$p/__event/gpx$q")
                    .openConnection() as java.net.HttpURLConnection
                c.requestMethod = "POST"
                c.doOutput = true
                c.connectTimeout = 20000
                c.readTimeout = 90000
                c.setRequestProperty("Content-Type", "application/gpx+xml")
                c.setFixedLengthStreamingMode(bytes.size)
                c.outputStream.use { it.write(bytes) }
                val code = c.responseCode
                val txt = (if (code in 200..299) c.inputStream else c.errorStream)
                    ?.bufferedReader()?.use { it.readText() } ?: ""
                c.disconnect()
                val o = JSONObject(txt)
                ui {
                    if (o.optBoolean("ok")) {
                        val off = if (o.optBoolean("ascent_off"))
                            "（与官方 +${o.optInt("official_ascent")} m 差 >10%，以官方为准）"
                        else ""
                        toast("已导入 ${o.optDouble("km")} km / +${o.optInt("ascent")} m$off")
                        if (this::web.isInitialized) {
                            // 跳回「刚导入的那一场」，不是按日期猜的下一场
                            val sl = o.optString("slug", "")
                            val go = if (sl.isNotBlank())
                                "/event?slug=" + java.net.URLEncoder.encode(sl, "UTF-8") +
                                    "&imported=1"
                            else "/event?imported=1"
                            web.loadUrl("http://127.0.0.1:$p$go")
                        }
                    } else {
                        AlertDialog.Builder(this).setTitle("导入失败")
                            .setMessage(o.optString("error", "未知错误"))
                            .setPositiveButton("好", null).show()
                    }
                }
            } catch (t: Throwable) {
                ui {
                    AlertDialog.Builder(this).setTitle("导入失败")
                        .setMessage(t.message ?: t.javaClass.simpleName)
                        .setPositiveButton("好", null).show()
                }
            }
        }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    /**
     * 顶部进度线。`pct` 给了就走确定进度，没给就是跑马灯。
     *
     * 以前是居中大圆圈：出回放要几分钟，页面被盖住、点不动，看着像死机。
     * 现在它只占 2dp，用户该干嘛干嘛，进度靠「产物」页和标题栏看。
     */
    private fun busy(on: Boolean, pct: Int = -1) {
        if (!on) {
            bar.visibility = View.GONE
            bar.isIndeterminate = true
            nudgeHandler.removeCallbacks(nudge)
            return
        }
        bar.visibility = View.VISIBLE
        if (pct in 0..100) {
            bar.isIndeterminate = false
            bar.progress = pct
        } else {
            bar.isIndeterminate = true
        }
        // 生成期间定时戳一下页面：WebView 在空闲/不可见时会节流 JS 定时器，
        // 用户「不摸屏幕进度就不动」就是这么来的。页面没有 gcTick 时是空操作。
        nudgeHandler.removeCallbacks(nudge)
        nudgeHandler.postDelayed(nudge, 1200)
    }

    private val nudgeHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val nudge = object : Runnable {
        override fun run() {
            try {
                web.evaluateJavascript("window.gcTick&&window.gcTick()", null)
            } catch (_: Throwable) {
            }
            nudgeHandler.postDelayed(this, 1200)
        }
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
            // 启动失败时立刻撤掉开屏，否则错误对话框被这层盖住，用户只看到图标。
            splash?.let { (it.parent as? ViewGroup)?.removeView(it) }
            splash = null
            splashGone = true
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
        const val REQ_BUNDLE = 1002
        const val REQ_MEAL_PICK = 1003
        const val REQ_MEAL_SHOT = 1004
        /** GPX 必须让开 1003/1004（1.0.8 拍照识热量已占用） */
        const val REQ_GPX = 1005
        /** 赛事资料附件上传（PDF / 图片 / 文本） */
        const val REQ_ATTACH = 1006
        const val REQ_FILE_CHOOSER = 1007

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
