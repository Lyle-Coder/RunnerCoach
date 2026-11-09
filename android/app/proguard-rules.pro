# R8 保留下来的东西。
#
# 这个 App 有一半逻辑在网页里，通过 `GC.action('...')` / `GC.activity(...)`
# 回调原生 —— 调用点只存在于 JS 字符串里，R8 静态分析看不见，默认会把
# `MainActivity$Bridge` 的方法当成「没人用」删掉。删掉之后网页不报错、
# 按钮也没反应（v0.4.3 那次空引用事故就是这么来的），所以这里显式钉住。

# 网页桥：所有 @JavascriptInterface 方法
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keep class mm.runnercoach.app.MainActivity$Bridge { *; }

# Chaquopy：Python 运行时通过 JNI + 反射访问这些类
-keep class com.chaquo.python.** { *; }
-dontwarn com.chaquo.python.**

# 保留行号，崩溃栈还能对上源码
-keepattributes SourceFile,LineNumberTable
