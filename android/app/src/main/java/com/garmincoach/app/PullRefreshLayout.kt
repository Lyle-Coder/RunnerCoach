package com.garmincoach.app

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.content.Context
import android.util.AttributeSet
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.animation.DecelerateInterpolator
import android.widget.FrameLayout
import android.widget.TextView
import kotlin.math.min

/**
 * 极简下拉刷新：只在内容已经滚到顶、手指继续下拉时才接管手势。
 *
 * 为什么不引 androidx.swiperefreshlayout：这个 App 刻意不依赖任何 AndroidX
 * 组件（纯 android.app.Activity + WebView），为一条手势多拖一堆依赖不划算，
 * 而且我们要的反馈文案是「下拉同步」而不是一个转圈。
 *
 * 刷新过程由外部用 setRefreshing(true/false) 驱动，本类不碰业务。
 */
class PullRefreshLayout(context: Context, attrs: AttributeSet? = null) :
    FrameLayout(context, attrs) {

    private val hintH = dp(52)
    private val trigger = dp(36)      // 阻尼后的位移阈值，相当于手指下拉 72dp
    private val maxPull = dp(66)      // 阻尼后的最大位移
    private val slop = ViewConfiguration.get(context).scaledTouchSlop

    private val hint = TextView(context).apply {
        text = "↓ 下拉同步"
        textSize = 13f
        setTextColor(0xFF5A6B5E.toInt())
        gravity = Gravity.CENTER
        setBackgroundColor(0xFFF6F7F4.toInt())
        elevation = 8f
        translationY = -hintH.toFloat()
    }

    private var target: View? = null
    private var downY = 0f
    private var dragging = false
    private var refreshing = false
    private var finishing = false
    private var offset = 0
    private var anim: ValueAnimator? = null

    /** 松手越过阈值时回调；业务侧要在结束时调 setRefreshing(false)。 */
    var onRefresh: (() -> Unit)? = null

    init {
        setBackgroundColor(0xFFF6F7F4.toInt())
        addView(hint, LayoutParams(LayoutParams.MATCH_PARENT, hintH, Gravity.TOP))
    }

    /** 告诉容器哪个子 View 是会被下移的内容（避免和指示器、进度圈混淆）。 */
    fun attach(v: View) {
        target = v
    }

    private fun canScrollUp(): Boolean = (target?.scrollY ?: 0) > 0

    override fun onInterceptTouchEvent(ev: MotionEvent): Boolean {
        if (refreshing || target == null) return false
        when (ev.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downY = ev.y
                dragging = false
            }
            MotionEvent.ACTION_MOVE -> {
                val dy = ev.y - downY
                // 只看向下的手势，且必须是页面已经到顶、再也滚不动了
                if (dy > slop && !canScrollUp()) {
                    dragging = true
                    return true
                }
            }
        }
        return super.onInterceptTouchEvent(ev)
    }

    override fun onTouchEvent(ev: MotionEvent): Boolean {
        if (refreshing) return false
        when (ev.actionMasked) {
            MotionEvent.ACTION_MOVE -> {
                if (dragging) {
                    val dy = (ev.y - downY).coerceAtLeast(0f)
                    // 阻尼：拖 2px 只下移 1px，手感不会像页面被拽走
                    offset = min(dy * 0.5f, maxPull.toFloat()).toInt()
                    applyOffset()
                    return true
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (dragging) {
                    dragging = false
                    if (offset >= trigger) beginRefresh() else settle(0)
                    return true
                }
            }
        }
        return super.onTouchEvent(ev)
    }

    private fun beginRefresh() {
        refreshing = true
        finishing = false
        hint.text = "正在同步…"
        settle(hintH)
        onRefresh?.invoke()
    }

    /** 程序侧也能起刷新（首次登录后的自动同步）。 */
    fun setRefreshing(on: Boolean) {
        if (on) {
            if (refreshing) return
            finishing = false
            refreshing = true
            hint.text = "正在同步…"
            settle(hintH)
        } else if (refreshing) {
            refreshing = false
            finishing = true
            hint.text = "√ 已更新"
            // 立即回弹：留着位移会干扰下一次手势的起点计算
            settle(0) {
                finishing = false
                hint.text = "↓ 下拉同步"
            }
        }
    }

    /** 手势拉开了但又不需要同步（例如没登录、要去弹登录框）。 */
    fun cancel() {
        if (!refreshing) return
        refreshing = false
        finishing = false
        settle(0)
    }

    private fun applyOffset() {
        target?.translationY = offset.toFloat()
        hint.translationY = offset - hintH.toFloat()
        if (!refreshing && !finishing) {
            hint.text = if (offset >= trigger) "↑ 松开立即同步" else "↓ 下拉同步"
        }
    }

    private fun settle(to: Int, onEnd: (() -> Unit)? = null) {
        anim?.cancel()
        anim = ValueAnimator.ofInt(offset, to).apply {
            duration = 220
            interpolator = DecelerateInterpolator()
            addUpdateListener {
                offset = it.animatedValue as Int
                applyOffset()
            }
            if (onEnd != null) {
                addListener(object : AnimatorListenerAdapter() {
                    override fun onAnimationEnd(animation: Animator) {
                        onEnd()
                    }
                })
            }
            start()
        }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
}
