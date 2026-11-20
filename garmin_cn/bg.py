# -*- coding: utf-8 -*-
"""后台补传：把本地库里还没传上去的组数据补发一次。

原生侧 JobScheduler 每 6 小时唤起一次，App 回到前台再补一次（nudge）。
入口只有 `catch_up()`，别在别处另写一套上报逻辑——小组上报的规则
（首轮 30 天、之后 14 天、地图与动态是全量覆盖）都在 `group.report_group`，
这里只负责「什么时候调、调几次、失败怎么记」。

为什么不顺手去佳明拉数据
------------------------
App 的数据源是手机上的 SQLite。后台要拉佳明就得有会话，而 JWT 几小时
就失效、项目刻意不存密码（`_remember_email` 只记邮箱）。后台硬拉的结果
是绝大多数时候卡在 401，还要多背一份重登与验证码分支。
所以补传只解决一件事：**跑完了、本地有了、但没打开组页，队友看不到**。
真拉到新数据仍然靠用户打开 App（前台同步本来就会做）。

隐私口径
--------
地图格子只在本地缓存明确记着「这个组开着」时才发（`group._map_on`）。
缓存是 None（这台设备从没进过这个组的页面）时，这里**先拉一次 detail
补缓存**，而不是默认当成开着——宁可少传，也不能因为一个后台任务把位置
悄悄传上去。
"""
from __future__ import annotations

import datetime as dt
import threading

__all__ = ["catch_up", "status", "KEY_AT", "KEY_RES", "MIN_GAP"]

# 同进程互斥：JobService 与前台页面可能同时触发。Python 侧只有一个
# runtime（Chaquopy 的 Python 是进程级单例），所以内存锁足够。
_LOCK = threading.Lock()

KEY_AT = "bg_last_at"          # 上次真正跑完的时间（ISO）
KEY_RES = "bg_last_res"        # 上次结果，形如 "2/2"

# 距上次不到这么久就跳过。前台来回切不该反复上报：一次全量覆盖虽然幂等，
# 但白耗流量，队友的榜单也会被无谓刷新。
MIN_GAP = 20 * 60


def status(store) -> dict:
    """上次补传的时间和结果，给「诊断日志」/设置页显示。

    只读本地 meta，不联网。没跑过时返回空字段，由界面决定怎么说。
    """
    try:
        at = store.get_meta(KEY_AT) or ""
        res = store.get_meta(KEY_RES) or ""
    except Exception:      # noqa: BLE001 - 读不到就当没跑过
        at, res = "", ""
    return {"ok": True, "at": at, "result": res,
            "due": _due(at)}


def catch_up(store=None, today: str | None = None, force: bool = False) -> dict:
    """补传一轮。失败不抛，异常一律落 diag 并返回 ok=false。

    `force=True` 跳过「刚跑过」的节流，只给手动验证用。
    `store` 传进来就用它（调用方负责关），不传就自己开自己关。
    """
    if not _LOCK.acquire(blocking=False):
        return {"ok": True, "skip": "busy"}
    try:
        return _run(store, today, force)
    except Exception as e:      # noqa: BLE001 - 后台任务不能崩
        from . import diag

        diag.log_exc("bg.catch_up", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _LOCK.release()


# ---------------------------------------------------------------- 内部
def _due(at: str) -> bool:
    """距上次够久了就该跑。时间解析不了（老库/被改坏）按该跑处理。"""
    if not at:
        return True
    try:
        last = dt.datetime.fromisoformat(str(at))
    except ValueError:
        return True
    return (dt.datetime.now() - last).total_seconds() >= MIN_GAP


def _run(store, today: str | None, force: bool) -> dict:
    from . import account
    from . import diag
    from . import group as G
    from .store import Store

    if not account.logged_in():
        # 没登录就没有组，静默跳过：这不是错误，不该每 6 小时写一条日志
        return {"ok": True, "skip": "no_account"}

    own = store is None
    if own:
        from . import appapi as A

        store = Store(A.DB)
    try:
        at = store.get_meta(KEY_AT) or ""
        if not force and not _due(at):
            return {"ok": True, "skip": "recent", "at": at}

        day = today or dt.date.today().isoformat()
        lst = G.groups()
        if not lst.get("ok"):
            # token 过期也会走到这里。不写 KEY_AT：失败不该把节流推后，
            # 否则用户重登后要等 20 分钟才轮到下一轮。
            return {"ok": False, "error": lst.get("error") or "拉不到小组列表"}

        gids = [int(x["id"]) for x in (lst.get("groups") or []) if x.get("id")]
        sent = failed = 0
        for gid in gids:
            try:
                _prime_map(store, gid)
                r = G.report_group(store, gid, day, first=not G.seen(gid, store))
                if r.get("ok"):
                    sent += 1
                else:
                    failed += 1
            except Exception as e:      # noqa: BLE001 - 一个组坏了不耽误别的组
                diag.log_exc(f"bg.report:{gid}", e)
                failed += 1

        # 队列里还压着更早失败的行，顺手再试一次（没到退避时间的会自动跳过）
        try:
            G.flush(store, day)
        except Exception as e:      # noqa: BLE001
            diag.log_exc("bg.flush", e)

        stamp = dt.datetime.now().isoformat(timespec="seconds")
        try:
            store.set_meta(KEY_AT, stamp)
            store.set_meta(KEY_RES, f"{sent}/{len(gids)}")
        except Exception:      # noqa: BLE001 - 记账失败不影响本次已传上去的数据
            pass
        return {"ok": True, "groups": len(gids), "sent": sent,
                "failed": failed, "at": stamp}
    finally:
        if own:
            try:
                store.conn.close()
            except Exception:      # noqa: BLE001
                pass


def _prime_map(store, gid: int) -> None:
    """本地还没记过这个组的开关时，拉一次 detail 补上。

    缓存成 "0"（明确关着）和没有记录（从没进过这组页面）是两件事：
    前者要尊重，后者要查清楚。`group._map_on` 两者都返回 False，
    所以在它之前补这一步，不然新设备永远传不上足迹。
    """
    try:
        if store.get_meta(f"group_map_{int(gid)}") is not None:
            return
    except Exception:      # noqa: BLE001
        return
    from . import group as G

    d = G.detail(gid)
    if not d.get("ok"):
        return
    me = d.get("me") or {}
    if "map_share" in me:
        G.note_map(store, gid, bool(me.get("map_share")))
