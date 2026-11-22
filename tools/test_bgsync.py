"""后台补传的离线回归：只查源码与清单，不跑 Android、不联网。

为什么用静态断言守
------------------
这条链路的每一环都在别的文件里，少一环不会报错，只会「任务排上了但永远
不跑」——那种故障在真机上表现为「队友就是看不到我的里程」，靠肉眼很难
归因。所以把已经踩过的坑固化成断言：

- `SyncJob` 少 `BIND_JOB_SERVICE` → 系统绑定抛 SecurityException，静默不跑
- `setPersisted(true)` 缺 `RECEIVE_BOOT_COMPLETED` → schedule 直接抛
- Kotlin 调错函数名 → Python 侧返回 `没有这个接口`，日志里才有痕迹
- `catch_up` 里若出现 `_client(` → 说明有人把「去佳明拉数据」加回来了，
  那会重新引入 401 与验证码分支，违背这一版「只补传本地」的约定

    python tools/test_bgsync.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AND = ROOT / "android" / "app" / "src" / "main"
MANIFEST = (AND / "AndroidManifest.xml").read_text(encoding="utf-8")
BGSYNC_KT = (AND / "java" / "mm" / "runnercoach" / "app" / "BgSync.kt").read_text(
    encoding="utf-8")
SYNCJOB_KT = (AND / "java" / "mm" / "runnercoach" / "app" / "SyncJob.kt").read_text(
    encoding="utf-8")
NOTIFY_KT = (AND / "java" / "mm" / "runnercoach" / "app" / "Notify.kt").read_text(
    encoding="utf-8")
MAIN_KT = (AND / "java" / "mm" / "runnercoach" / "app" / "MainActivity.kt").read_text(
    encoding="utf-8")
BG_PY = (ROOT / "garmin_cn" / "bg.py").read_text(encoding="utf-8")


class ManifestTest(unittest.TestCase):
    def test_service_declared(self):
        m = re.search(r'<service[^>]*android:name="\.SyncJob"(?P<rest>[^>]*)/?>', MANIFEST)
        self.assertIsNotNone(m, "Manifest 里没有 .SyncJob，任务排上了也没人来跑")
        body = MANIFEST[m.start():m.start() + 400]
        self.assertIn("BIND_JOB_SERVICE", body,
                      "SyncJob 缺 BIND_JOB_SERVICE，系统绑定时会抛 SecurityException")
        self.assertIn('android:exported="false"', body, "SyncJob 不该对外暴露")

    def test_boot_permission(self):
        self.assertIn("RECEIVE_BOOT_COMPLETED", MANIFEST,
                      "setPersisted(true) 需要这条权限，否则 schedule 直接抛异常")


class KotlinTest(unittest.TestCase):
    def test_period_is_half_hour(self):
        # 原先是 6 小时。改成 30 分钟是为了「跑完不久收到一条」：佳明不给 Push，
        # 只能自己按周期去问。改这个值要连 BgSync 的类注释一起改，
        # 并且想清楚耗电——每轮真正打网络的闸门在 bg.py 的 BG_SYNC_GAP。
        self.assertRegex(BGSYNC_KT, r"PERIOD_MS\s*=\s*30\s*\*\s*60\s*\*\s*1000L",
                         "周期不是 30 分钟，改这里要连文档一起改")

    def test_schedule_is_idempotent(self):
        self.assertIn("getPendingJob(JOB_PERIODIC)", BGSYNC_KT,
                      "少了 getPendingJob 检查，每次启动都会排一个新的，堆一堆重复任务")

    def test_persisted(self):
        self.assertIn("setPersisted(true)", BGSYNC_KT,
                      "重启后任务就没了：用户不重启还好，一重启彻底断掉")

    def test_network_constraint(self):
        self.assertIn("NETWORK_TYPE_ANY", BGSYNC_KT, "没网时也会被唤起，白耗电")

    def test_calls_python_wrong_name_guard(self):
        self.assertIn('"bg_catch_up"', SYNCJOB_KT,
                      "SyncJob 调的名字必须与 appapi._API 里的一致")
        self.assertIn("jobFinished", SYNCJOB_KT,
                      "不调 jobFinished，系统会一直持有唤醒锁")

    def test_wired_into_activity(self):
        self.assertIn("BgSync.schedule(this)", MAIN_KT, "启动时没排周期任务")
        self.assertIn("onResume", MAIN_KT, "缺 onResume，回前台不会补传")
        self.assertIn("BgSync.nudge(this)", MAIN_KT, "onResume 里没排补传")


class PythonTest(unittest.TestCase):
    def test_api_whitelisted(self):
        from garmin_cn import appapi as A

        for name in ("bg_catch_up", "bg_status"):
            self.assertIn(name, A._API, f"{name} 没进 _API 白名单，App 调会报「没有这个接口」")

    def test_throttle(self):
        self.assertRegex(BG_PY, r"MIN_GAP\s*=\s*20\s*\*\s*60",
                         "节流不是 20 分钟")
        self.assertIn("force", BG_PY, "缺 force 就没法手动验证")

    def test_no_garmin_client(self):
        # 这一版刻意不碰佳明账号：会话几小时就失效、又不存密码，
        # 后台硬拉只会卡在 401。出现 _client( 说明有人把它加回来了。
        self.assertNotIn("_client(", BG_PY,
                         "catch_up 不该去佳明拉数据，见 bg.py 文件头")

    def test_no_account_is_quiet(self):
        self.assertIn("no_account", BG_PY,
                      "没登录要静默跳过，不能每轮往 diag.log 写一条")

    def test_failure_keeps_stamp(self):
        # 失败不写 KEY_AT：否则用户重登后要白等一个节流周期
        self.assertIn("KEY_AT", BG_PY)
        self.assertNotRegex(BG_PY, r"return \{[^}]*error[^}]*\}\s*\n\s*stamp",
                            "失败路径不该落节流时间")


class NotifyTest(unittest.TestCase):
    """小组通知这条链：bg.news → SyncJob → Notify → 点通知回到那个组。

    每一环单独看都没错，少一环的表现是「后台跑了、通知没出现」或者
    「通知出现了、点了没反应」，都很难从现象反推。
    """

    def test_channel_and_permission(self):
        self.assertIn("CH = \"group_news\"", NOTIFY_KT, "通知渠道名变了要一起改")
        self.assertIn("IMPORTANCE_DEFAULT", NOTIFY_KT,
                      "渠道重要度低于 DEFAULT 就不会弹出来，等于没通知")
        self.assertIn("POST_NOTIFICATIONS", NOTIFY_KT,
                      "13+ 没权限时 notify 会被静默丢弃，必须自己先查")

    def test_grouped_by_group(self):
        self.assertIn("byGid", NOTIFY_KT,
                      "不按组汇总会按事件一条一条弹，11 人的组一天能刷满通知栏")
        self.assertIn("notify(gid", NOTIFY_KT,
                      "通知 id 用 gid：同组只留最新一条，不然会堆成一列")

    def test_click_opens_group(self):
        self.assertIn("putExtra(\"gid\"", NOTIFY_KT, "通知里没带组 id")
        self.assertIn("getIntExtra(\"gid\"", MAIN_KT, "MainActivity 没读组 id")
        self.assertIn("openGroup(", MAIN_KT, "读了 gid 却没跳转")

    def test_emitted_from_sync_job(self):
        self.assertIn("Notify.emit", SYNCJOB_KT,
                      "补传跑完没发通知，news 白拉了")
        self.assertIn("\"news\"", SYNCJOB_KT, "没从 Python 结果里取 news 字段")

    def test_teammate_run(self):
        self.assertIn('"act" ->', NOTIFY_KT,
                      "队友跑步没有通知文案分支，会被归到兜底那句")
        self.assertIn('Locale.US', NOTIFY_KT,
                      "没用固定 Locale，某些区域设置会把小数点写成逗号")
        self.assertIn('"km"', BG_PY, "没把里程传给原生")

    def test_python_side(self):
        self.assertIn("KEY_NEWS", BG_PY, "通知游标没地方存")
        self.assertIn("on_my_act", BG_PY, "评论「是不是在我动态下」没传给原生")
        # 第一次只取起点：装完就弹一屏三个月前的评论，用户会直接关通知
        self.assertIn("if since:", BG_PY, "第一次拉取就返回了历史条目")


class BehaviourTest(unittest.TestCase):
    """跑一遍真实入口：没登录时必须是安静跳过，且不写节流时间。"""

    def test_skip_without_account(self):
        import tempfile

        from garmin_cn import account as A
        from garmin_cn import bg as B
        from garmin_cn.store import Store

        old = A.logged_in
        d = Path(tempfile.mkdtemp())
        st = Store(d / "t.db")
        try:
            A.logged_in = lambda: False
            r = B.catch_up(store=st, force=True)
            self.assertEqual(r.get("skip"), "no_account")
            self.assertFalse(st.get_meta(B.KEY_AT), "没登录却记了「上次跑过」")
        finally:
            A.logged_in = old
            st.conn.close()
            import shutil

            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
