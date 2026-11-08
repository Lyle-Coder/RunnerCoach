"""佳明国区 · 每日看板 / 电子教练 命令行入口。

    run.bat login          首次登录（支持邮箱验证码）
    run.bat sync --days 30 同步数据
    run.bat report         生成看板
    run.bat demo           用仿真数据预览看板
    run.bat status         查看状态
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

HOME = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME))

from garmin_cn.auth import AuthError, GarminCNAuth, MfaRequired  # noqa: E402
from garmin_cn.client import GarminCNClient, SessionExpired  # noqa: E402
from garmin_cn.coach import context_markdown  # noqa: E402
from garmin_cn.metrics import derive  # noqa: E402
from garmin_cn.report import render  # noqa: E402
from garmin_cn.store import Store  # noqa: E402
from garmin_cn.sync import SyncService  # noqa: E402
from garmin_cn.sysopen import open_any  # noqa: E402

CONFIG = HOME / "config.json"
DB = HOME / "data" / "garmin.db"
OUT = HOME / "output"


def _setup_io() -> None:
    """保证输出立刻可见（不被缓冲吞掉），编码异常也不至于直接崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, errors="replace")
        except Exception:
            pass


def _prompt(text: str, secret: bool = False) -> str:
    """读一行输入；终端不支持隐藏输入时自动退回明文，EOF 时给明确提示。"""
    try:
        if secret:
            import getpass

            try:
                if not sys.stdin.isatty():
                    raise OSError("stdin 不是交互终端")
                return getpass.getpass(text).strip()
            except (EOFError, OSError, KeyboardInterrupt):
                pass  # 重定向 / 部分 IDE 控制台不支持隐藏输入，退回明文
        return input(text).strip()
    except EOFError:
        print("\n× 没读到输入：这个窗口不是交互式的（或被重定向了）。")
        print("  改用一次性传参：run.bat login --email 你的邮箱 --password 你的密码")
        raise SystemExit(2)


def load_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG.chmod(0o600)
    except Exception:
        pass


def _auto_login(cfg: dict) -> bool:
    """用已保存的账号密码续期会话（国区 JWT_WEB 只有 2 小时）。"""
    email, pwd = cfg.get("email"), cfg.get("password")
    if not (email and pwd):
        return False
    auth = GarminCNAuth(HOME)
    try:
        auth.login(email, pwd)
        return True
    except MfaRequired:
        print("× 这次登录需要邮箱验证码，无法自动续期 → 请运行：run.bat login")
        return False
    except Exception as e:
        print(f"× 自动续期失败：{e}")
        return False


def get_client(require: bool = True) -> GarminCNClient:
    from garmin_cn.net import Net

    auth = GarminCNAuth(HOME)
    net = Net(auth.session_path, min_interval=0.5)
    has_session = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))
    if not has_session:
        cfg = load_config()
        if _auto_login(cfg):
            net = Net(auth.session_path, min_interval=0.5)
            has_session = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))
    if not has_session:
        if require:
            raise AuthError("还没有登录（会话里没有 JWT_WEB），先运行：run.bat login")
        raise AuthError("未登录")
    return GarminCNClient(net)


# ---------------- 命令 ----------------
def cmd_login(args) -> int:
    print("佳明国区登录 · connect.garmin.cn")
    print("=" * 46)
    cfg = load_config()

    email = args.email or cfg.get("email") or _prompt("Garmin 国区账号邮箱: ")
    if not email:
        print("× 邮箱不能为空")
        return 2
    password = args.password or cfg.get("password")
    if not password:
        password = _prompt("密码（输入时不显示）: ", secret=True)
    if not password:
        print("× 密码不能为空")
        return 2

    auth = GarminCNAuth(HOME)
    print(f"→ ① 向 sso.garmin.cn 提交登录（{email}）…", flush=True)
    try:
        result = auth.login(email, password)
    except MfaRequired:
        print("→ ② 需要邮箱验证码：Garmin 已发到注册邮箱（也翻一下垃圾箱）", flush=True)
        code = _prompt("邮箱验证码: ")
        if not code:
            print("× 验证码不能为空")
            return 2
        print("→ ③ 校验验证码…", flush=True)
        try:
            result = auth.submit_mfa(code)
        except AuthError as e:
            print(f"× {e}")
            return 2
    except AuthError as e:
        print(f"× {e}")
        names = ", ".join(auth.net.cookie_names()) or "无"
        print(f"  Cookie：{names}")
        print("  想定位原因可以跑：run.bat debug-login（输出里不含密码）")
        if args.verbose:
            traceback.print_exc()
        return 2
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as e:  # 网络 / 解析等意外
        print(f"× 登录异常：{type(e).__name__}: {e}")
        if args.verbose:
            traceback.print_exc()
        else:
            print("  加 --verbose 看完整堆栈")
        return 4

    cfg.update({"email": email, "password": password})
    save_config(cfg)
    p = result.get("profile") or {}
    name = " ".join(x for x in (p.get("displayName"), p.get("fullName")) if x) or email
    print(f"√ 登录成功：{name}")
    print("  会话已保存到 data/session.json（等同密码，不要提交到 git）")
    print("  下一步：run.bat sync --days 30  然后  run.bat report --open")
    return 0


def cmd_debug_login(args) -> int:
    """登录诊断：把每一步 HTTP 状态、Cookie、接口返回都打出来。"""
    print("佳明国区登录 · 诊断模式")
    print("=" * 46)
    cfg = load_config()
    email = args.email or cfg.get("email") or _prompt("Garmin 国区账号邮箱: ")
    password = args.password or cfg.get("password") or _prompt("密码（输入时不显示）: ", secret=True)
    if not email or not password:
        print("× 邮箱和密码都要有")
        return 2

    auth = GarminCNAuth(HOME)
    auth.net.min_interval = 0.3
    auth.net.trace = lambda s: print(s, flush=True)
    ok = False
    try:
        res = auth.login(email, password)
        ok = True
        print(f"√ 登录成功：{(res.get('profile') or {}).get('displayName')}")
    except MfaRequired:
        print("· 需要邮箱验证码，诊断模式不处理验证码，请改用 run.bat login")
    except AuthError as e:
        print(f"× {e}")
    except Exception as e:
        print(f"× {type(e).__name__}: {e}")

    print("-" * 46)
    print(f"Cookie：{', '.join(auth.net.cookie_names()) or '无'}")

    from garmin_cn.client import GarminCNClient, SessionExpired

    client = GarminCNClient(auth.net)
    for path in ("/userprofile-service/socialProfile", "/userprofile-service/userprofile/settings"):
        try:
            data = client.get(path)
            head = json.dumps(data, ensure_ascii=False)[:120] if data else "空"
            print(f" {path} → HTTP {client.last_status}  {head}")
        except SessionExpired as e:
            print(f" {path} → 被拒绝 {e}")

    if not ok:
        print("-" * 46)
        print("对比：去掉 Sec-Fetch-Site 再请求一次（应看到 403，说明网关在拦）")
        auth.net.request("GET", "https://connect.garmin.cn/gc-api/userprofile-service/socialProfile",
                         headers={"Accept": "application/json"}, retries=1)
        print("可以把这个输出发我，里面有足够定位问题的信息（不含密码）。")
        return 2

    auth.net.save_session()
    print("会话已保存，下一步：run.bat sync --days 30")
    return 0


def cmd_import_cookies(args) -> int:
    raw = args.cookie or _prompt("粘贴 connect.garmin.cn 的 Cookie 字符串: ")
    auth = GarminCNAuth(HOME)
    try:
        res = auth.import_cookies(raw)
    except AuthError as e:
        print(f"× {e}")
        return 2
    print(f"√ 已导入 {res['cookies']} 条 Cookie，账号：{res['profile'].get('displayName')}")
    return 0


def cmd_sync(args) -> int:
    try:
        client = get_client()
    except AuthError as e:
        print(f"× {e}")
        return 2
    store = Store(DB)
    svc = SyncService(client, store)

    def progress(i, n, d):
        print(f"\r  同步 {i}/{n} {d}", end="", flush=True)

    force = bool(getattr(args, "force", False))
    days = args.days
    end = getattr(args, "end", None)
    pages = getattr(args, "activity_pages", None)
    start = getattr(args, "start", None)
    if start:
        try:
            s = dt.date.fromisoformat(start)
            e = dt.date.fromisoformat(end) if end else dt.date.today()
        except ValueError:
            print("× --start / --end 必须是 YYYY-MM-DD")
            return 2
        if s > e:
            print("× 起始日不能晚于结束日")
            return 2
        days = (e - s).days + 1
        if days > 1095:
            print("× 一次最多补 3 年（1095 天），请分段拉取")
            return 2
        end = e.isoformat()
        if pages is None:
            pages = min(20, max(1, (days + 29) // 30))
    if pages is None:
        pages = 1
    try:
        res = svc.sync(days=days, end=end, extras=args.extras,
                       progress=progress, force=force, activity_pages=pages)
    except SessionExpired:
        print("\n· 会话过期，尝试用已保存的账号自动续期…", flush=True)
        if not _auto_login(load_config()):
            print("× 需要重新登录：run.bat login")
            return 3
        client = get_client()
        svc = SyncService(client, store)
        try:
            res = svc.sync(days=days, end=end, extras=args.extras,
                           progress=progress, force=force,
                           activity_pages=pages)
        except SessionExpired:
            print("\n× 续期后仍然被拒绝，请重新登录：run.bat login")
            return 3
    print()
    skipped = res.get("skipped", 0)
    tail = f"，跳过已缓存 {skipped} 天" if skipped else ""
    print(f"√ {res['range']}：拉取 {res['fetched']} 天{tail}，"
          f"{res['activities']} 条活动，共 {res.get('requests', 0)} 次请求")
    if not getattr(args, "no_cards", False):
        try:
            made = auto_cards(store, client, days=min(days, 7))
        except Exception as e:
            made = []
            print(f"  活动图生成跳过：{type(e).__name__}: {e}")
        for p in made:
            print(f"√ 活动图已生成：{p}")
    if res["errors"]:
        print("  部分接口异常（不影响主流程）：")
        for e in res["errors"]:
            print(f"   - {e}")
    # 体重 / 体成分：秤的数据不在佳明云里，每次同步顺手带一次（失败不阻断主流程）
    if getattr(args, "body", True):
        _sync_body(store, days=min(max(days, 7), 120))
    return 0


def _sync_body(store, days: int = 30) -> int:
    """顺手拉小米秤的体重/体成分，并回填进 daily。

    没登录、云端没记录都只提示一句：主流程（佳明数据）已经成功，体重是附加项。
    """
    from garmin_cn import mi as M

    try:
        n, recs = M.sync(store, HOME, days=days)
    except M.MiAuthError as e:
        print(f"· 体重未同步：{e}")
        return 0
    except Exception as e:
        print(f"· 体重未同步：{type(e).__name__}: {e}")
        return 0
    if n:
        print(f"√ 体重/体成分 {n} 条（最新 {recs[-1]['date']}）")
    else:
        print("· 体重：云端这段时间没有新记录（上秤称一次再同步）")
    return n


def _refresh_live(store, date: str) -> None:
    """出图/出报告前把「会随时间变」的指标重新拉一次。

    两样：身体电量（佳明，日内一直在变，daily summary 里的最高值不动会误导）、
    体重体成分（小米秤，不在佳明云里，只有 sync 才拉——早上称完不同步，
    看板就会一直拿昨天的数当「最新」）。失败都静默跳过，用库里上一次的值。
    """
    from garmin_cn import live

    client = None
    try:
        client = get_client()
    except Exception:
        client = None
    got = live.body_battery(store, date, client)
    if got.get("live"):
        print(f"· 身体电量已刷新：{got.get('now')}%（{got.get('now_at') or '—'}）")

    sc = live.scale(store, HOME)
    if sc.get("live") and sc.get("latest"):
        w = f"{sc['weight']:.2f} kg" if sc.get("weight") else ""
        print(f"· 体重已刷新：{w}（{sc['latest']}）")


def _build_report(date: str | None, out: Path) -> Path:
    store = Store(DB)
    date = date or dt.date.today().isoformat()
    _refresh_live(store, date)
    res = derive(store, date)
    path = render(res, store, out)
    return path


def cmd_report(args) -> int:
    store = Store(DB)
    if store.stats()["days"] == 0:
        print("× 库里还没有数据，先运行：run.bat sync --days 30")
        return 2
    date = args.date or dt.date.today().isoformat()
    out = OUT / f"dashboard-{date}.html"
    path = _build_report(date, out)
    shutil.copyfile(path, OUT / "latest.html")
    # 同步一份到发布目录，这样重新发布时上传的就是最新看板
    site_dir = HOME / "site"
    if site_dir.is_dir():
        shutil.copyfile(path, site_dir / "index.html")
    print(f"√ 看板已生成：{path}")
    if args.open:
        open_any(path)  # type: ignore[attr-defined]
    return 0


def cmd_card(args) -> int:
    """一图流：把当天快照渲染成一张竖版 PNG（没有无头浏览器时退化成 HTML）。"""
    from garmin_cn.card import render as render_card
    from garmin_cn.plan import attach

    store = Store(DB)
    if store.stats()["days"] == 0:
        print("× 库里还没有数据，先运行：run.bat sync --days 30")
        return 2
    date = args.date or dt.date.today().isoformat()
    _refresh_live(store, date)
    res = attach(derive(store, date))
    out = OUT / f"card-{date}.png"
    try:
        path, is_png = _shot(render_card, out, res, sync_at=store.get_meta("last_sync"))
    except Exception as e:
        print(f"× 出图失败：{type(e).__name__}: {e}")
        return 3
    if not is_png:
        shutil.copyfile(path, OUT / "card.html")
        _no_browser_note(path)
        if args.open:
            open_any(path)
        return 0
    shutil.copyfile(path, OUT / "card.png")
    site_dir = HOME / "site"
    if site_dir.is_dir():
        shutil.copyfile(path, site_dir / "card.png")
    print(f"√ 一图流已生成：{path}")
    print(f"  尺寸约 {path.stat().st_size // 1024} KB")
    if args.open:
        open_any(path)
    return 0


RUN_TYPES = ("running", "trail_running", "treadmill_running", "track_running",
             "virtual_run", "indoor_running", "obstacle_run", "ultra_run")


def _shot(fn, out: Path, *a, **kw):
    """出 PNG；没有无头浏览器时退化成同目录的 HTML。

    两个 render 都是「先写 HTML、再调浏览器截图」，所以截图失败时 HTML 一定在，
    这里只要把那个文件交出去就行。返回 (路径, 是不是 PNG)。
    """
    try:
        return fn(*a, out, **kw), True
    except RuntimeError as e:
        msg = str(e)
        if "Chrome" not in msg and "浏览器" not in msg:
            raise
        html = Path(out).with_suffix(".html")
        if not html.exists():
            raise
        return html, False


def _no_browser_note(path: Path) -> None:
    print(f"√ 已生成 HTML 版：{path}")
    print("  这台机器没有无头浏览器（Termux / 精简 Windows 常见），所以不出 PNG。")
    print("  手机浏览器打开就是整张竖版，长按存图即可发微信。")


def auto_cards(store, client, days: int = 7) -> list[Path]:
    """给最近 days 天里还没出过图的跑步活动补一张活动图（本地，不发布）。"""
    from garmin_cn import activity as act
    from garmin_cn.actcard import render as render_act
    from garmin_cn.card import can_shot

    if not can_shot():
        # 没有无头浏览器（Termux / 精简 Windows）：不出图，也不逐条报错刷屏。
        # 单次活动的 HTML 用 `run.bat act --card` 时仍会生成。
        print("  · 没有无头浏览器，跳过活动图（看板与回放不受影响）")
        return []

    end = dt.date.today()
    start = end - dt.timedelta(days=max(1, days))
    made: list[Path] = []
    for row in reversed(store.activities(start.isoformat(), end.isoformat())):
        if (row.get("type") or "") not in RUN_TYPES:
            continue
        out = OUT / f"act-{row['date']}-{row['activity_id']}.png"
        if out.exists():
            continue
        try:
            detail = act.ensure(store, client, row["activity_id"])
            render_act(row, detail, out)
            shutil.copyfile(out, OUT / "act.png")
            made.append(out)
        except Exception as e:
            print(f"  × 活动图失败（{row['date']} {row['name']}）：{type(e).__name__}: {e}")
    return made


def cmd_act(args) -> int:
    """单次活动：抓分圈/时序/轨迹/天气，修正步频步幅后落库，可顺手出图。"""
    from garmin_cn import activity as act

    store = Store(DB)
    row = store.get_activity(activity_id=args.id, date=args.date)
    if not row:
        hint = f"id={args.id}" if args.id else f"日期 {args.date or dt.date.today().isoformat()}"
        print(f"× 库里没有找到活动（{hint}），先运行：run.bat sync --days 7")
        return 2
    try:
        client = get_client(require=True)
        detail = act.ensure(store, client, row["activity_id"], force=args.force)
    except Exception as e:
        print(f"× 抓取明细失败：{type(e).__name__}: {e}")
        return 3

    s = detail["summary"]
    w = detail.get("weather") or {}
    print(f"√ {row['name']}（{row['date']}）")
    print(f"  步频 {s['cad_avg']} spm（P95 {s['cad_p95']}，最大 {s['cad_max']}）"
          f"｜步幅 {s['stride_avg']} cm｜跑动样本 {s['run_samples']}")
    print(f"  步频分布 {s['cad_bands']}")
    if s.get("drift"):
        d = s["drift"]
        print(f"  前后半程：心率 {d['hr_first']}→{d['hr_second']}，配速 {d['pace_first']}→{d['pace_second']} s/km")
    if w:
        print(f"  天气 {w.get('temp_c')}℃（体感 {w.get('feels_c')}℃）湿度 {w.get('humidity')}% {w.get('text')}")
    print(f"  分圈 {len(detail['laps'])}｜轨迹点 {len(detail['track'])}｜时序点 {len(detail['series']['points'])}")

    if args.card:
        from garmin_cn import privacy as PV
        from garmin_cn.actcard import render as render_act
        mode = PV.normalize(args.privacy)
        suffix = "" if mode == PV.MODE_NONE else f"-{mode}"
        out = OUT / f"act-{row['date']}-{row['activity_id']}{suffix}.png"
        try:
            path, is_png = _shot(render_act, out, row, detail,
                                 title=args.title, privacy=mode)
            if mode != PV.MODE_NONE:
                print(f"√ 分享脱敏：{PV.note(mode)}")
        except Exception as e:
            print(f"× 出图失败：{type(e).__name__}: {e}")
            return 4
        if is_png:
            shutil.copyfile(path, OUT / "act.png")
            print(f"√ 活动图已生成：{path}")
        else:
            _no_browser_note(path)
        if args.open:
            open_any(path)
    return 0


def cmd_race(args) -> int:
    """赛事成绩：录入官方成绩、列出个人最好成绩、出赛后复盘图。"""
    store = Store(DB)

    # ---- 录入官方成绩 ----
    if args.add:
        date = args.date or dt.date.today().isoformat()
        event = args.event or "比赛"
        aid = args.id or ""
        if not aid:
            row = store.get_activity(date=date)
            aid = row["activity_id"] if row else ""
        rid = store.add_race_result(
            date=date, event=event, bib=args.bib or "",
            activity_id=aid, gun_s=args.gun, net_s=args.net,
            distance_km=args.dist, target_s=args.target, note=args.note or "")
        print(f"√ 已录入 {date} {event} 官方成绩（id={rid}）")
        if args.net:
            print(f"  净成绩 {_hms(args.net)}" + (f"｜枪声 {_hms(args.gun)}" if args.gun else ""))

    # ---- 个人最好成绩 ----
    if args.best or not (args.add or args.card):
        pbs = store.personal_bests()
        if not pbs:
            print("· 还没有录入任何官方成绩（用 --add 录一场）")
        else:
            print("√ 个人最好成绩：")
            for r in pbs:
                t = r.get("net_s") or r.get("gun_s")
                tgt = r.get("target_s")
                gap = ""
                if tgt and t:
                    gap = f"　较目标 {t - tgt:+.0f} 秒"
                print(f"  {r['date']} {r['event']}　{_hms(t)}"
                      f"　({r.get('distance_km') or 0:.2f} km){gap}")

    # ---- 赛后复盘图 ----
    if args.card:
        from garmin_cn import activity as act
        from garmin_cn.racecard import render as render_race
        from garmin_cn import raceplan

        date = args.date or dt.date.today().isoformat()
        race = store.race_result(date, args.event)
        if not race:
            print(f"× {date} 没有录入官方成绩，先用 --add 录入")
            return 2
        aid = race.get("activity_id") or ""
        row = store.get_activity(activity_id=aid) if aid else None
        if not row:
            row = store.get_activity(date=date)
        if not row:
            print(f"× 库里没有找到 {date} 的活动")
            return 2
        try:
            client = get_client(require=True)
            detail = act.ensure(store, client, row["activity_id"])
        except Exception as e:
            print(f"× 抓取明细失败：{type(e).__name__}: {e}")
            return 3

        # 赛后一周课表：比赛次日（周一）起 7 天
        week = []
        for k in range(7):
            d = dt.date.fromisoformat(date) + dt.timedelta(days=1 + k)
            info = raceplan.day_info(d) or {}
            week.append({
                "dow": info.get("dow") or ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][k],
                "main": info.get("abbr") or "休",
                "sub": info.get("sub") or "",
                "kind": info.get("kind") or "rest",
            })

        out = OUT / f"race-{date}.png"
        try:
            path, is_png = _shot(render_race, out, race, detail, week,
                                 coach_text=args.coach or "", row=row)
        except Exception as e:
            print(f"× 出图失败：{type(e).__name__}: {e}")
            return 4
        if is_png:
            shutil.copyfile(path, OUT / "race.png")
            print(f"√ 赛后复盘图已生成：{path}")
        else:
            _no_browser_note(path)
        if args.open:
            open_any(path)
    return 0


def _hms(sec) -> str:
    if sec is None:
        return "—"
    s = int(round(sec))
    h, m, ss = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m}:{ss:02d}"


def cmd_motion(args) -> int:
    """动态轨迹回放：合成轨迹 → 取底图 → 出 1080×1920 播放器（可再导 MP4）。"""
    from garmin_cn import activity as act, clip as clp, motion as mot, photo as ph
    store = Store(DB)
    row = store.get_activity(activity_id=args.id, date=args.date)
    if not row:
        hint = f"id={args.id}" if args.id else f"日期 {args.date or dt.date.today().isoformat()}"
        print(f"× 库里没有找到活动（{hint}），先运行：run.bat sync --days 7")
        return 2
    aid = str(row["activity_id"])

    detail = store.get_detail(aid)
    has_route = bool(detail and (detail.get("route") or {}).get("n"))
    if args.force or not has_route:
        if detail and not has_route:
            print("· 这条活动的明细是旧格式（没有轨迹时间轴），重抓一次…")
        try:
            client = get_client(require=True)
            detail = act.ensure(store, client, aid, force=True)
        except Exception as e:
            print(f"× 抓取明细失败：{type(e).__name__}: {e}")
            return 3

    folder = Path(args.photos) if args.photos else (HOME / "data" / "photos" / aid)
    photos = ph.scan(folder) if folder.exists() else []
    videos = clp.scan(folder, cache=HOME / "data" / "motion_cache") if folder.exists() else []
    if args.photos and not photos and not videos:
        print(f"! 目录里没读到照片/视频：{folder}")
    elif not folder.exists():
        print(f"· 没找到照片目录 {folder}（可选，跑完把原图放进去即可）")
    bad = [v for v in videos if v.get("error")]
    for v in bad:
        print(f"! 跳过 {v['name']}：{v['error']}")
    videos = [v for v in videos if not v.get("error")]

    out = Path(args.out) if args.out else (OUT / f"motion-{row['date']}-{aid}.html")
    if out.suffix.lower() == ".mp4":      # --out x.mp4 时别把网页写进 .mp4
        out = out.with_suffix(".html")
    try:
        html = mot.render(row, detail, photos or None, out,
                          duration=args.duration, thumb_dir=OUT / "thumbs",
                          clips=videos or None,
                          clip_cache=HOME / "data" / "motion_cache")
    except ValueError as e:
        print(f"× {e}")
        return 4

    side = html.with_suffix(".json").read_text("utf-8")
    meta = json.loads(side)
    print(f"√ {row['name']}（{row['date']}）")
    _nclip = sum(1 for p in meta["photos"] if p.get("clip"))
    _tail = f"（轨迹 {args.duration:.0f} 秒 + {_nclip} 段素材停留）" if _nclip else ""
    print(f"  {meta['dist'] / 1000:.2f} km · {meta['dur'] // 60} 分 "
          f"{meta['dur'] % 60} 秒 · 成片 {meta.get('total') or args.duration:.0f} 秒{_tail}")
    print(f"  轨迹点 {(detail.get('route') or {}).get('n')}｜照片 {len(photos)} 张"
          f"｜实况 {len(videos)} 段，匹配 {len(meta['photos'])} 处")
    for p in meta["photos"]:
        if p.get("clip"):
            print(f"    · {p['d'] / 1000:.2f} km 处实况（{p['clip']}）")
        elif p["n"]:
            print(f"    · {p['d'] / 1000:.2f} km 处 {p['n']} 张")
    if meta["loose"]:
        print(f"  未匹配 {len(meta['loose'])} 张：")
        for x in meta["loose"][:6]:
            print(f"    - {x['name']}：{x['why']}")
        if len(meta["loose"]) > 6:
            print(f"    … 另有 {len(meta['loose']) - 6} 张")
    print(f"√ 播放器：{html}")

    if args.mp4:
        from garmin_cn import video

        dest = Path(args.out).with_suffix(".mp4") if args.out else html.with_suffix(".mp4")
        try:
            got = video.export(html, dest, duration=args.duration, fps=args.fps,
                               workers=args.workers or 0,
                               keep_frames=args.keep_frames,
                               mute=args.mute)
            mb = got.stat().st_size / 1024 / 1024
            print(f"√ 视频：{got}（{mb:.1f} MB）")
        except RuntimeError as e:
            print(f"× 导出视频失败：{e}")
            return 5
    if args.open:
        mot.open_in_browser(html)
    return 0


def _parse_ts(s: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s.strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


def cmd_ingest(args) -> int:
    """收件箱 → 自动认领到某次活动 → 出片。

    不管文件是从微信、飞书、数据线还是 AirDrop 来的，只要落进一个目录，
    就按「拍摄时间」去找最近的那次跑步——不用先告诉我是哪一次。
    """
    import types

    from garmin_cn import clip as clp, photo as ph

    inbox = Path(args.dir) if args.dir else (HOME / "data" / "inbox")
    if not inbox.exists():
        print(f"× 收件箱不存在：{inbox}")
        return 2

    cache = HOME / "data" / "motion_cache"
    clips = clp.scan(inbox, cache=cache)
    live_src = {str(Path(c["source"]).resolve()) for c in clips if c.get("source")}
    stills = [p for p in ph.scan(inbox)
              if str(Path(p["path"]).resolve()) not in live_src]
    items = clips + stills
    if not items:
        print(f"× 收件箱里没有可识别的照片/视频：{inbox}")
        return 2

    store = Store(DB)
    # 候选活动：文件时间前后 6 小时内的都算
    all_days = sorted({dt.date.fromtimestamp(
        (it.get("t_utc") or Path(it.get("path", "")).stat().st_mtime * 1000) / 1000
    ).isoformat() for it in items})
    acts: list[dict] = []
    for d in all_days:
        day = dt.date.fromisoformat(d)
        acts += store.activities((day - dt.timedelta(days=1)).isoformat(),
                                 (day + dt.timedelta(days=1)).isoformat())
    uniq = {}
    for a in acts:
        uniq[str(a["activity_id"])] = a
    acts = list(uniq.values())

    def window(a: dict) -> tuple[float, float]:
        st = _parse_ts(a.get("start_ts") or "")
        if not st:
            return (0.0, 0.0)
        t0 = st.timestamp()
        return t0 - 300, t0 + (a.get("duration_s") or 0) + 900

    plan: dict[str, list[dict]] = {}
    loose: list[tuple[str, str]] = []
    for it in items:
        path = Path(it.get("path") or "")
        t = it.get("t_utc")
        src = it.get("t_source") or ""
        if not t and path.exists():
            t, src = int(path.stat().st_mtime * 1000), "mtime"
        if not t:
            loose.append((it["name"], "读不到拍摄时间"))
            continue
        sec = t / 1000.0
        best, best_gap, best_shift = None, 1e18, 0
        for a in acts:
            lo, hi = window(a)
            if not lo:
                continue
            for shift in (0, 8, -8, 9, -9):
                cand = sec - shift * 3600
                if lo <= cand <= hi:
                    # 同一天可能跑两次：按时区惩罚 + 到开跑时刻的距离择优
                    score = abs(shift) * 3600 + abs(cand - (lo + 300))
                    if score < best_gap:
                        best, best_gap, best_shift = a, score, shift
        if not best:
            loose.append((it["name"], "附近没有匹配的活动"))
            continue
        plan.setdefault(str(best["activity_id"]), []).append(
            {**it, "_when": t, "_src": src, "_shift": best_shift})

    if not plan:
        print("× 没有一个文件能对上一次活动：")
        for n, why in loose:
            print(f"    {n}：{why}")
        return 3

    print(f"· 收件箱 {inbox}：{len(items)} 个文件 → {len(plan)} 次活动")
    for aid, rows in plan.items():
        a = next(x for x in acts if str(x["activity_id"]) == aid)
        print(f"  {a['date']} {a['name']}（{aid}）：{len(rows)} 个")
        for r in rows:
            when = dt.datetime.fromtimestamp(r["_when"] / 1000)
            tag = f"（{r['_src']}" + (f"，时区-{r['_shift']}h" if r["_shift"] else "") + "）"
            print(f"    · {r['name']}  {when:%m-%d %H:%M:%S} {tag}")

    if args.dry_run:
        print("· --dry-run：只报告，不复制不出片")
        return 0

    made = []
    for aid, rows in plan.items():
        dst_dir = HOME / "data" / "photos" / aid
        dst_dir.mkdir(parents=True, exist_ok=True)
        for r in rows:
            src = Path(r.get("source") or r["path"])
            dst = dst_dir / src.name
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            shutil.copy2(src, dst)
        print(f"· 归入 {dst_dir}（{len(rows)} 个）")
        ns = types.SimpleNamespace(
            id=aid, date=None, photos=None, out=None,
            duration=args.duration, mp4=not args.html_only,
            fps=args.fps, workers=args.workers or 0,
            keep_frames=args.keep_frames, mute=args.mute,
            force=False, open=False)
        print()
        cmd_motion(ns)
        made.append(aid)

    if loose:
        print("\n未归组：")
        for n, why in loose:
            print(f"    {n}：{why}")
    print(f"\n√ 完成 {len(made)} 次活动")
    return 0


def cmd_photo(args) -> int:
    """检查照片/视频的拍摄时间与 GPS，以及能不能挂到某次活动的轨迹上。"""
    from garmin_cn import clip as clp, photo as ph

    folder = Path(args.dir) if args.dir else (HOME / "data" / "photos")
    rows = ph.scan(folder)
    videos = clp.scan(folder, cache=HOME / "data" / "motion_cache")
    if not rows and not videos:
        print(f"× {folder} 下没有照片/视频（支持 jpg/jpeg/png/webp/mp4/mov）")
        return 2

    ok = [r for r in rows if r.get("lat") is not None and r.get("t_utc")]
    print(f"√ 扫到 {len(rows)} 张照片，其中 {len(ok)} 张带 GPS")
    if videos:
        have = sum(1 for v in videos if v.get("lat") is not None)
        print(f"√ 扫到 {len(videos)} 段视频（含实况），{have} 段带 GPS"
              f"——没有 GPS 的也能靠拍摄时间吸附")
    for r in rows:
        if r.get("error"):
            print(f"  × {r['name']}：{r['error']}")
            continue
        t = dt.datetime.fromtimestamp(r["t_utc"] / 1000) if r["t_utc"] else None
        mark = "√" if r.get("lat") is not None else "·"
        print(f"  {mark} {r['name']}  {t:%m-%d %H:%M:%S}（{r['t_source']}）"
              f"  {r['w']}×{r['h']}  "
              + (f"{r['lat']:.5f}, {r['lon']:.5f}" if r.get("lat") is not None else "无坐标"))

    if args.id or args.date:
        store = Store(DB)
        row = store.get_activity(activity_id=args.id, date=args.date)
        if not row:
            print("× 库里没有这次活动")
            return 3
        detail = store.get_detail(str(row["activity_id"]))
        rt = (detail or {}).get("route") or {}
        if not rt.get("n"):
            print("× 这次活动还没有轨迹时间轴，先运行：run.bat motion --id " + str(row["activity_id"]))
            return 4
        got = ph.match(rows, rt, t0_ms=rt.get("t0"))
        print(f"\n匹配 {row['name']}（{row['date']}）：{len(got['points'])} 处 / "
              f"{len(rows)} 张照片")
        if videos:
            vg = ph.match(videos, rt, t0_ms=rt.get("t0"), keep=("dur",))
            print(f"视频：{len(vg['points'])} 处 / {len(videos)} 段")
            for g in vg["points"]:
                gap = g.get("gap_m")
                where = "按时间定位" if gap is None else f"偏离 {gap:.0f} m"
                print(f"  √ {g['d'] / 1000:5.2f} km  时长 {g['shots'][0].get('dur')}s"
                      f"  {where}  {g['shots'][0]['name']}")
            got["loose"].extend(vg["loose"])
        for g in got["points"]:
            gap = g.get("gap_m")
            where = "按时间定位" if gap is None else f"偏离 {gap:.0f} m"
            print(f"  √ {g['d'] / 1000:5.2f} km  t={int(g['t']) // 60}:{int(g['t']) % 60:02d}"
                  f"  {where}  {len(g['shots'])} 张")
        for x in got["loose"]:
            print(f"  × {x['name']}：{x['why']}")
    return 0


def cmd_backfill(args) -> int:
    """批量补齐历史活动的明细（步频修正 + 轨迹 + 天气），供出图用。"""
    from garmin_cn import activity as act

    store = Store(DB)
    if args.force:
        rows = store.conn.execute(
            "SELECT activity_id, date, name FROM activities ORDER BY date DESC"
        ).fetchall()
    else:
        rows = store.conn.execute(
            "SELECT a.activity_id, a.date, a.name FROM activities a"
            " LEFT JOIN activity_detail d ON d.activity_id = a.activity_id"
            " WHERE d.activity_id IS NULL ORDER BY a.date DESC"
        ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("√ 所有活动明细都已补齐")
        return 0
    client = get_client(require=True)
    print(f"待补 {len(rows)} 次活动…")
    ok = fail = 0
    for i, r in enumerate(rows, 1):
        try:
            act.ensure(store, client, r["activity_id"], force=args.force)
            ok += 1
            print(f"  [{i}/{len(rows)}] √ {r['date']} {r['name']}")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(rows)}] × {r['date']} {type(e).__name__}: {e}")
    print(f"√ 完成：成功 {ok}，失败 {fail}")
    return 0 if fail == 0 else 1


def cmd_today(args) -> int:
    """今日 105K 备赛课次（与执行卡、逐日课表同源）。"""
    from garmin_cn import raceplan

    if not raceplan.available():
        print("× 本地课表未配置：需要 tools/make_plan.py 与 tools/make_card.py")
        print("  这两份是个人课表，仓库不含；没有它们会回退到佳明教练计划。")
        return 2
    d = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    if args.week:
        txt = raceplan.format_week(d)
        if txt.startswith("该日期"):
            print("× " + txt)
            return 2
        print(txt)
        return 0
    info = raceplan.day_info(d)
    if not info:
        s, e = raceplan.span()
        print(f"× {d} 不在 105K 备赛计划区间内（{s} – {e}）")
        return 2
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=1))
        return 0
    print(raceplan.format_day(info))
    for nxt in raceplan.upcoming(d, args.days)[1:]:
        print(f"明日：{nxt['abbr']}" + (f"  {nxt['sub']}" if nxt["sub"] else "")
              + f"　{nxt['tip']}")
    return 0


def cmd_push(args) -> int:
    """把 105K 课表推成佳明 Connect 训练课程，并排进日历（手表自动同步）。"""
    from garmin_cn import raceplan, workout

    if not raceplan.available():
        print("× 本地课表未配置：需要 tools/make_plan.py 与 tools/make_card.py")
        print("  这两份是个人课表，仓库不含；没有它们会回退到佳明教练计划。")
        return 2

    mp = raceplan._mp()
    today = dt.date.today()

    if args.all:
        start, end = raceplan.span()
    elif args.week:
        start = mp.date_of(args.week, 0)
        end = mp.date_of(args.week, 6)
    elif args.date:
        start = end = dt.date.fromisoformat(args.date)
    elif args.start or args.end:
        start = dt.date.fromisoformat(args.start) if args.start else today
        end = dt.date.fromisoformat(args.end) if args.end else start
    else:
        start, end = today, today + dt.timedelta(days=6)

    if start > end:
        start, end = end, start
    days = (end - start).days + 1
    print(f"区间 {start} → {end}（{days} 天）" + ("　[演练，不会真的提交]" if args.dry_run else ""))

    client = None
    if not args.dry_run:
        try:
            client = get_client(require=True)
        except AuthError as e:
            print(f"× {e}\n  先运行：run.bat login")
            return 3

    results = workout.push_range(client, start, end, dry=args.dry_run,
                                 replace=args.replace)
    ok = skip = fail = 0
    for r in results:
        if r.get("skipped"):
            skip += 1
            continue
        if r.get("error"):
            fail += 1
            print(f"  × {r['date']} {r['name']}　{r['error']}")
            continue
        ok += 1
        tail = ""
        if not args.dry_run:
            tail = f"  id={r.get('workoutId')}"
            tail += " · 已排程" if r.get("scheduled") else " · 排程失败"
        print(f"  √ {r['date']} {r['name']}{tail}")
        if args.dry_run and args.verbose:
            import json
            print(json.dumps(r["payload"], ensure_ascii=False, indent=1))

    if client is not None:
        try:
            plans = client.active_plans() or []
            if plans:
                names = "、".join(str(p.get("trainingPlanName") or p.get("name") or "?")
                                 for p in plans[:3])
                print(f"\n！佳明侧还有进行中的计划：{names}")
                print("  手表会同时显示两套课表，建议去 Connect → 训练计划 里结束它们，"
                      "只看日历里的 105K 课程。")
        except Exception:
            pass

    print(f"完成：建成 {ok}，跳过休息 {skip}，失败 {fail}")
    if fail:
        return 1
    return 0


def cmd_note(args) -> int:
    """把教练点评写进当天记录，看板会自动渲染。"""
    store = Store(DB)
    date = args.date or dt.date.today().isoformat()
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    else:
        text = (args.text or "").strip()
    if not text:
        print("× 点评内容为空（用 --text 或 --file）")
        return 2
    store.add_note(text, date=date, source="workbuddy")
    print(f"√ 已写入 {date} 的教练点评（{len(text)} 字）")
    print("  看板会在下次 report 时更新：run.bat report --open")
    return 0


def cmd_coach(args) -> int:
    store = Store(DB)
    date = args.date or dt.date.today().isoformat()
    from garmin_cn.plan import attach

    res = attach(derive(store, date))
    print(context_markdown(res))
    return 0


def cmd_plan(args) -> int:
    from garmin_cn.plan import fetch, item_line, load

    today = args.date or dt.date.today().isoformat()
    if getattr(args, "diag", False):
        try:
            client = get_client()
        except AuthError as e:
            print(f"× {e}")
            return 2
        from garmin_cn.plan import diag

        print(_json.dumps(diag(client, today), ensure_ascii=False, indent=1))
        return 0
    if args.cached:
        data = load(HOME / "data" / "plan.json")
        if not data:
            print("× 还没有计划快照，先跑：run.bat plan")
            return 2
    else:
        try:
            client = get_client()
        except AuthError as e:
            print(f"× {e}")
            return 2
        print("→ 读取佳明教练计划…", flush=True)
        data = fetch(client, today, out_path=HOME / "data" / "plan.json")

    if not data.get("available"):
        print(f"× {data.get('reason') or '没有可用的训练计划'}")
        return 1

    ph = data.get("phase") or {}
    print(f"\n【{data['name']}】{data.get('level') or ''} · {data.get('weeks')} 周 · "
          f"每周约 {data.get('per_week')} 次")
    print(f"计划期间：{data.get('start')} → {data.get('end')}"
          + (f"｜当前阶段：{ph.get('label')}（{ph.get('start')} ~ {ph.get('end')}）" if ph else ""))
    print()
    print("本周课表：")
    for d in data["week"]:
        if d["rest"]:
            print(f"  {d['date']} {d['dow']}  休息")
        else:
            for it in d["items"]:
                print(f"  {d['date']} {d['dow']}  {item_line(it)}")

    t = data.get("today") or {}
    print()
    if t.get("rest"):
        print("今天：佳明教练没有安排训练（休息日）。")
        if data.get("next_date"):
            nxt = "；".join(item_line(x) for x in data.get("next_items") or [])
            print(f"下一次：{data['next_date']} {nxt}")
    else:
        for it in t.get("items") or []:
            print(f"今天：{item_line(it)}")
    return 0


def _pick_activities(store, args) -> list[dict]:
    """按 --id / --date / --last 选活动，默认取最近几天的跑步。"""
    if getattr(args, "id", None):
        row = store.get_activity(activity_id=args.id)
        return [row] if row else []
    if getattr(args, "date", None):
        row = store.get_activity(date=args.date)
        return [row] if row else []
    today = dt.date.today()
    start = today - dt.timedelta(days=45)
    rows = sorted(store.activities(start.isoformat(), today.isoformat()),
                  key=lambda r: (r.get("start_ts") or r.get("date") or ""), reverse=True)
    if not getattr(args, "any", False):
        rows = [r for r in rows if (r.get("type") or "") in RUN_TYPES]
    return rows[:max(1, getattr(args, "last", 1) or 1)]


def _detail_of(store, client, row: dict) -> dict:
    from garmin_cn import activity as act

    return act.ensure(store, client, row["activity_id"])


def _card_of(row: dict, detail: dict) -> Path:
    """活动图：已有就复用（出图要跑一次无头浏览器，别白跑）。"""
    from garmin_cn.actcard import render as render_act

    out = OUT / f"act-{row['date']}-{row['activity_id']}.png"
    if out.exists() and out.stat().st_size > 1024:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    return render_act(row, detail, out)


def _join_briefs(items: list[dict]) -> str:
    """几条简报拼成一整块：正文之间用分隔线断开，图单独一行放在末尾。"""
    blocks = []
    for it in items:
        txt = it["text"]
        if it.get("image"):
            txt += f"\n图：{it['image']}"
        blocks.append(txt)
    return ("\n\n" + "·" * 14 + "\n\n").join(blocks)


def _mi_verify(cloud, M, tries: int = 3) -> bool:
    """走完小米的安全验证：先图形验证码换短信，再提交短信码。

    小米对「新设备 / 异地」的密码登录会返回 `securityStatus=16` + 一个
    `notificationUrl`，但那个页面只有短信一条路（options=[4]），没有「手机点确认」。
    所以这两关绕不过去，只是能把它自动化到「看图输字 + 输短信码」。
    """
    v = cloud._verify or {}
    label = "邮件" if str(v.get("flag")) == "8" else "短信"
    if str(v.get("flag")) != "4":
        print(f"× 这个账号要求的是「{label}验证」，自动流程只做了短信这一条。")
        print("  改走 --json-file：F12 → Network → 过滤 serviceLoginAuth2 → 点最后一条")
        print("  → Response Headers 整段复制存成 mi_login.txt → run.bat mi login --json-file mi_login.txt")
        return False
    print(f"→ 安全验证：{label}会发到 {v.get('masked', '绑定的手机号')}")

    sent = False
    for _ in range(tries):
        try:
            img = cloud.captcha()
        except M.MiAuthError as e:
            print(f"  × {e}")
            return False
        p = HOME / "data" / "captcha.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(img)
        open_any(p)  # 图形验证码要人认字，Termux 里交给相册打开
        print(f"  图形验证码 → {p}")
        icode = _prompt("  图里的字符（直接回车换一张）: ")
        if not icode:
            continue
        try:
            r = cloud.send_code(icode)
        except M.MiError as e:
            print(f"  × {e}")
            return False
        if str(r.get("code")) == "0":
            print(f"√ {label}已发出，去看手机")
            sent = True
            break
        print(f"  × {r.get('desc') or r.get('reason') or r.get('code')}")

    if not sent:
        print("× 图形验证码连续不对，重新跑 run.bat mi login 再来一次")
        return False

    for _ in range(3):
        code = _prompt(f"  {label}验证码: ")
        if not code:
            return False
        try:
            r = cloud.verify_code(code)
        except M.MiCodeError as e:
            print(f"  × {e}")   # 码错了才重输，码是一次性的，别让用户白等第二条短信
            continue
        except M.MiAuthError as e:
            print(f"  × {e}")
            return False
        except M.MiError as e:
            print(f"  × 连不上小米账号：{e}")
            return False
        uid = r.get("userId") or cloud.user_id
        print(f"√ 验证通过（userId={uid}）")
        if r.get("relogged"):
            print("  已用同一账号自动重登，签名密钥拿到")
        return True
    return False


def cmd_mi(args) -> int:
    """小米健康云：登录、体检、看原始样本。

    ms116（米家体脂秤 S400）本身不存历史——秤只做蓝牙广播，历史全在云端。
    所以「能不能取到」比「取到多少」更关键，status 会把几个时间窗的命中数都打出来。
    """
    from garmin_cn import mi as M

    cloud = M.MiCloud(HOME)
    act = (args.action or "status").lower()

    if act == "logout":
        cloud.clear()
        print("√ 已删除 data/mi_auth.json（云端数据不受影响）")
        return 0

    if act == "login":
        jraw = args.json_text or ""
        if getattr(args, "json_file", None):
            p = Path(args.json_file)
            if not p.exists():
                print(f"× 文件不存在：{p}")
                return 2
            jraw = p.read_text(encoding="utf-8-sig").strip()
        if jraw:
            print("→ 解析登录材料（JSON / 响应头）…", flush=True)
            try:
                r = cloud.import_login_json(jraw)
            except M.MiAuthError as e:
                print(f"× {e}")
                return 2
            except M.MiError as e:
                print(f"× 换 serviceToken 失败：{e}")
                return 3
            print(f"√ 会话已建立（userId={r['userId']}），凭据存 data/mi_auth.json")
            print("  下一步：run.bat mi status")
            return 0

        raw = args.cookie
        if getattr(args, "cookie_file", None):
            p = Path(args.cookie_file)
            if not p.exists():
                print(f"× 文件不存在：{p}")
                return 2
            raw = p.read_text(encoding="utf-8-sig").strip()
        if raw:
            try:
                cloud.import_cookie(raw)
            except M.MiAuthError as e:
                print(f"× {e}")
                return 2
            print("√ 已保存 Cookie 凭据（整串照收，不挑字段）")
        else:
            user = args.user or _prompt("小米账号（手机号 / 邮箱 / 小米 ID）: ")
            if not user:
                print("× 账号不能为空")
                return 2
            pwd = args.password or _prompt("密码（输入时不显示）: ", secret=True)
            if not pwd:
                print("× 密码不能为空")
                return 2
            print("→ 向 account.xiaomi.com 提交登录…", flush=True)
            try:
                r = cloud.login(user, pwd)
            except M.MiNeedVerify as e:
                print(f"→ {e}")
                if not _mi_verify(cloud, M):
                    print("  没走完安全验证。再跑一次 run.bat mi login 也行；")
                    print("  或者用浏览器路线：F12 → Network → 过滤 serviceLoginAuth2 →")
                    print("  最后一条的 Response Headers 整段存成 mi_login.txt → ")
                    print("  run.bat mi login --json-file mi_login.txt")
                    return 2
                r = {"verified": True}
            except M.MiAuthError as e:
                print(f"× {e}")
                if cloud.diag:
                    print(f"  登录返回：{cloud.diag}")
                if getattr(cloud, "notify_url", ""):
                    print(f"  验证页：{cloud.notify_url}")
                print("  兜底路线（不用猜 Cookie）：")
                print("   1. 浏览器打开（已登录会直接显示一段 JSON，未登录先登录）")
                print(f"      {M.ACCOUNT}/pass/serviceLogin?_json=true&sid={M.SID}&_locale=zh_CN")
                print("   2. Ctrl+A / Ctrl+C 把整页内容存成 mi_login.json")
                print("   3. run.bat mi login --json-file mi_login.json")
                return 2
            except M.MiError as e:
                print(f"× 登录失败：{e}")
                return 2
            if r.get("partial"):
                print("⚠ 二次验证没走完，但 passToken 已经能用；失效就重新登录")
            print("√ 登录成功，凭据存 data/mi_auth.json（不含密码）")

    if not cloud.load() and not (cloud.user_id or cloud.cookie_raw):
        print("× 还没登录小米账号 → run.bat mi login")
        return 2

    print(f"→ 换会话（userId={cloud.user_id or '未知'}）…", flush=True)
    try:
        fresh = cloud.open_session()
    except M.MiAuthError as e:
        print(f"× {e}")
        return 3
    except M.MiError as e:
        print(f"× 连不上小米健康云：{e}")
        return 3
    if not fresh:
        print("⚠ 这次没能刷新会话，改用上次存下的 ssecurity（短时可用）")

    try:
        info = cloud.probe()
    except M.MiError as e:
        print(f"× 读取失败：{e}")
        return 3

    hit = next((w for w in info["windows"] if w["count"]), None)
    if not hit:
        if info.get("alive"):
            print(f"√ 账号和会话都没问题：近 30 天有 {info['alive']} 条步数记录")
            print("  但「小米运动健康」这份云里**一条体重都没有**，所以不是登录问题。")
            print("  99% 是秤没绑到这个 App 的这个账号：")
            print("   ① 打开手机上的「米家」看有没有这台秤——有，数据在米家云，")
            print("      两朵云不互通，需要在「小米运动健康」里重新绑一次这台秤")
            print("   ② 用的是 Zepp Life（小米运动）——那是另一套云，也要重绑")
            print("   ③ 秤绑的是另一个小米账号（家里人的），换账号再登录")
            print("  确认后称一次，并打开「小米运动健康」让它上传，再跑 run.bat mi status")
            return 2
        print("√ 凭据有效，但这个账号云上没有任何数据（连步数也没有）")
        print("  说明这不是称重那个小米账号，换账号重登：run.bat mi login")
        return 2

    print(f"√ 凭据有效：最近 {hit['days']} 天内有 {hit['count']} 条体重记录")
    recs = M.to_records([info["sample"]])
    if recs:
        r = recs[0]
        bits = [f"{r['weight_kg']:.1f} kg"]
        if r["body_fat_pct"]:
            bits.append(f"体脂 {r['body_fat_pct']:.1f}%")
        if r["muscle_kg"]:
            bits.append(f"肌肉 {r['muscle_kg']:.1f} kg")
        if r["water_pct"]:
            bits.append(f"水分 {r['water_pct']:.0f}%")
        print(f"  最近一次：{r['ts']}　" + "　".join(bits))
    if args.raw or act == "probe":
        print("  原始字段：")
        print("  " + json.dumps(info["sample"], ensure_ascii=False)[:600])
    print("\n下一步：run.bat body --mi   （拉最近 120 天入库）")
    return 0


def cmd_coros(args) -> int:
    """高驰：登录 / 状态 / 同步（Training Hub Web API 数据源）。"""
    from garmin_cn import coros as C

    act = (args.action or "status").lower()
    cloud = C.CorosCloud(HOME)

    if act == "logout":
        cloud.clear()
        print("√ 已删除 data/coros_auth.json")
        return 0

    if act == "login":
        user = args.user or _prompt("高驰账号邮箱: ")
        pwd = args.password or _prompt("密码（输入时不显示）: ", secret=True)
        region = args.region or "asia"
        print(f"→ 登录高驰（{C.REGION_CN.get(region, region)}）…", flush=True)
        try:
            r = cloud.login(user, pwd, region)
        except C.CorosError as e:
            print(f"× {e}")
            return 2
        print(f"√ 登录成功（userId={r['user_id']}），凭据存 data/coros_auth.json（不含密码）")
        return 0

    if not cloud.load():
        print("× 还没登录高驰账号 → run.bat coros login")
        return 2
    print(f"√ 已登录（userId={cloud.user_id}，{C.REGION_CN.get(cloud.region)}）")

    if act == "sync":
        from garmin_cn.store import Store
        store = Store(DB)
        try:
            r = C.sync(store, HOME, days=args.days,
                       email=args.user, password=args.password)
        except C.CorosError as e:
            print(f"× 同步失败：{e}")
            return 2
        tail = "（已静默重登）" if r.get("relogged") else ""
        print(f"√ {r['range']}：每日指标 {r['daily']} 天，活动 {r['activities']} 条{tail}")
    return 0


def cmd_body(args) -> int:
    """体重 / 体成分：导入、查看趋势、与训练负荷的联合判断。"""
    from garmin_cn import body as B

    store = Store(DB)
    B.ensure(store)

    if getattr(args, "import_file", None):
        p = Path(args.import_file)
        if not p.exists():
            print(f"× 文件不存在：{p}")
            return 2
        n, recs = B.import_file(store, p, source=args.source or "csv")
        if not n:
            print("× 没解析出任何记录，检查表头：日期 / 体重 / 体脂率 / 肌肉量 / 水分率")
            print("  认得的列名见 garmin_cn/body.py 的 ALIASES")
            return 2
        days = sorted({r["date"] for r in recs})
        print(f"√ 导入 {n} 条（{days[0]} ~ {days[-1]}，来源 {args.source or 'csv'}）")

    if getattr(args, "garmin", False):
        try:
            client = get_client(require=True)
        except AuthError as e:
            print(f"× {e}")
            return 3
        try:
            n = B.import_garmin(store, client)
        except SessionExpired:
            # Cookie 还在但服务端已判过期：用存好的凭据续一次，别让用户去点登录
            cfg = load_config()
            if not _auto_login(cfg):
                print("× 会话已过期，续登失败 → run.bat login")
                return 3
            n = B.import_garmin(store, get_client(require=True))
        print(f"√ 从佳明拉取 weigh-in {n} 条")

    if getattr(args, "mi", False):
        from garmin_cn import mi as M

        days = getattr(args, "mi_days", 120)
        print(f"→ 从小米健康云拉取最近 {days} 天体重 / 体成分…", flush=True)
        try:
            n, recs = M.sync(store, HOME, days=days)
        except M.MiAuthError as e:
            print(f"× {e}")
            return 3
        except M.MiError as e:
            print(f"× 小米云端读取失败：{e}")
            return 3
        if not n:
            print("× 云端这段时间没有体重记录。")
            print("  先跑 run.bat mi status：它会用步数判断账号到底通没通。")
            print("    有步数 → 账号没问题，是秤没绑在「小米运动健康」，去 App 里重绑这台秤")
            print("    没步数 → 登的不是称重那个小米账号")
            return 2
        d0, d1 = recs[0]["date"], recs[-1]["date"]
        print(f"√ 从小米云端入库 {n} 条（{d0} ~ {d1}）")

    st = B.stats(store, args.date, args.days)
    print()
    print(B.text(st, st.get("ctl_delta")))
    if st["count"] > 1:
        print(f"\n记录 {st['count']} 次称重，跨度 {st['span_days']} 天"
              f"（7 天均值 {st['avg7'] or '—'} kg，前 7 天 {st['avg_prev7'] or '—'} kg）")
    return 0


def cmd_nutri(args) -> int:
    """今天吃多少：由体重 / 体成分 / 训练量推出热量与三大营养素目标。"""
    from garmin_cn import nutrition as N

    if getattr(args, "goal", None):
        if not N.save_goal(args.goal):
            print(f"× 不认识的目标：{args.goal}")
            return 2
        print(f"√ 身体目标已设为「{N.GOALS[args.goal]['label']}」，写入 config.json，"
              f"看板与教练上下文都会跟着变")

    store = Store(DB)
    date = args.date or dt.date.today().isoformat()
    nt = (derive(store, date) or {}).get("nutrition") or {}
    print(N.text(nt))
    if nt.get("ok"):
        p, c, f = N.pct_split(nt)
        print(f"\n配比 {p}/{c}/{f}（蛋白/碳水/脂肪，按热量计），"
              f"合计 {nt['protein_kcal'] + nt['carb_kcal'] + nt['fat_kcal']} kcal")
        print(f"依据：体重 {nt['weight']} kg｜消耗基线 {nt['burn_base']} kcal"
              f"（{nt['burn_days']} 天日均{'，估算' if nt['estimated'] else ''}）"
              f"｜日均训练 {nt['avg_train_min']} 分钟"
              f"（4 周 {nt['train_28']} / 7 天 {nt['train_7']}）")
    return 0


def cmd_brief(args) -> int:
    """把活动压成一段能直接发进微信的纯文本（+ 一张活动图）。"""
    from garmin_cn import brief

    store = Store(DB)
    rows = _pick_activities(store, args)
    if not rows:
        hint = f"id={args.id}" if getattr(args, "id", None) else (
            f"日期 {args.date}" if getattr(args, "date", None) else "最近 45 天")
        print(f"× 没找到活动（{hint}），先运行：run.bat sync --days 7")
        return 2

    need_net = any(not store.get_detail(str(r["activity_id"])) for r in rows)
    client = None
    if need_net:
        try:
            client = get_client(require=True)
        except AuthError as e:
            print(f"× 需要联网抓明细，但{e}")
            print("  先运行：run.bat login")
            return 3

    items = []
    for row in rows:
        try:
            detail = _detail_of(store, client, row)
        except SessionExpired:
            print("× 会话过期，抓不到明细：run.bat login")
            return 3
        except Exception as e:
            print(f"× {row.get('date')} 明细失败：{type(e).__name__}: {e}")
            continue
        img = ""
        if not args.no_card:
            try:
                img = str(_card_of(row, detail))
            except Exception as e:
                print(f"  活动图跳过：{type(e).__name__}: {e}")
        items.append({"id": row["activity_id"], "date": row.get("date"),
                      "type": row.get("type"), "image": img,
                      "text": brief.one(row, detail)})

    if args.json:
        print(json.dumps({"items": items}, ensure_ascii=False, indent=1))
        return 0
    print(_join_briefs(items))
    if args.open and items and items[0]["image"]:
        open_any(items[0]["image"])
    return 0


def cmd_watch(args) -> int:
    """检查有没有「新跑完」的活动；有新活动就出图 + 出简报（给定时任务用）。

    幂等靠 `meta` 表里的 `pushed:<活动ID>`：标记过就不再重复推，
    所以这个命令可以每 10 分钟跑一次，不会刷屏。
    """
    from garmin_cn import brief
    from garmin_cn import activity as act

    store = Store(DB)
    client = None
    if not args.no_sync:
        try:
            client = get_client()
        except AuthError as e:
            print(f"· 未登录，跳过同步（{e}），只用库里已有数据")
            client = None
        if client is not None:
            svc = SyncService(client, store)
            try:
                res = svc.sync(days=2)
                print(f"· 同步 {res['range']}：{res['activities']} 条活动", flush=True)
            except SessionExpired:
                if _auto_login(load_config()):
                    client = get_client()
                    try:
                        res = SyncService(client, store).sync(days=2)
                        print(f"· 续期后同步：{res['activities']} 条活动", flush=True)
                    except SessionExpired:
                        print("· 续期后仍被拒绝，本次只用库里数据")
                        client = None
                else:
                    print("· 会话过期且自动续期失败，本次只用库里数据（run.bat login）")
                    client = None
            except Exception as e:
                print(f"· 同步出错（继续用库里数据）：{type(e).__name__}: {e}")

    now = dt.datetime.now()
    # 库里 start_ts 是「2026-11-02 07:12:00」（空格分隔），别拿 isoformat 的 T 去比：
    # 空格(32) < 'T'(84)，同一天的活动会被整批过滤掉。
    cutoff = (now - dt.timedelta(hours=max(1, args.hours))).strftime("%Y-%m-%d %H:%M:%S")
    today = now.date().isoformat()
    start = (now.date() - dt.timedelta(days=max(2, args.hours // 24 + 2))).isoformat()
    rows = [r for r in store.activities(start, today)
            if (r.get("type") or "") in RUN_TYPES
            and (r.get("start_ts") or "") >= cutoff]
    rows.sort(key=lambda r: r.get("start_ts") or "")

    fresh = [r for r in rows if not store.get_meta(f"pushed:{r['activity_id']}")]
    if not fresh:
        print(f"· 最近 {args.hours} 小时没有新的跑步活动")
        return 0

    out = []
    for row in fresh:
        try:
            if client is None:
                client = get_client(require=True)
            detail = act.ensure(store, client, row["activity_id"])
        except Exception as e:
            print(f"× {row.get('date')} 明细失败：{type(e).__name__}: {e}")
            continue
        img = ""
        if not args.no_card:
            try:
                img = str(_card_of(row, detail))
            except Exception as e:
                print(f"  活动图跳过：{type(e).__name__}: {e}")
        out.append({"id": row["activity_id"], "date": row.get("date"),
                    "image": img, "text": brief.one(row, detail)})
        if not args.dry_run:
            store.set_meta(f"pushed:{row['activity_id']}",
                           now.isoformat(timespec="seconds"))

    if not out:
        print("· 有新活动，但明细没抓到（多半是会话过期）")
        return 3
    if args.json:
        print(json.dumps({"items": out}, ensure_ascii=False, indent=1))
        return 0
    print()
    print(_join_briefs(out))
    return 0


def cmd_meal(args) -> int:
    """记一餐或看当天摄入；有记录时一图流会把「已吃 / 目标」并排显示。"""
    from garmin_cn import nutrition as nutr

    date = args.date or dt.date.today().isoformat()
    if args.add:
        if not args.kcal:
            print("× 用 --kcal 给个热量，其它三项（--p / --c / --f）能填就填，方便对账")
            return 2
        got = nutr.add_meal(date, args.add, args.kcal, args.p, args.c, args.f, args.time)
        r = got["row"]
        print(f"√ 已记录 {r['time']} {r['name']}：{r['kcal']} kcal"
              f"（蛋白 {r['protein_g']} g / 碳水 {r['carb_g']} g / 脂肪 {r['fat_g']} g）")

    eat = nutr.intake(date)
    if not eat["ok"]:
        print(f"{date} 还没有饮食记录")
        return 0
    print(f"{date} 已吃 {eat['kcal']} kcal｜蛋白 {eat['protein_g']} g｜"
          f"碳水 {eat['carb_g']} g｜脂肪 {eat['fat_g']} g（{len(eat['items'])} 餐）")

    try:
        nt = (derive(Store(DB), date).get("nutrition") or {})
    except Exception as e:
        print(f"（算目标失败：{type(e).__name__}: {e}）")
        return 0
    if nt.get("ok"):
        left = nt["target_kcal"] - eat["kcal"]
        state = f"还可吃 {left}" if left >= 0 else f"已超 {-left}"
        print(f"目标 {nt['target_kcal']} kcal（蛋白 {nt['protein_g']} / 碳水 {nt['carb_g']} / "
              f"脂肪 {nt['fat_g']} g）｜{state} kcal")
    return 0


def cmd_sleepfix(args) -> int:
    """矫正某晚实际睡眠时段（手表整晚没戴 / 掉落时佳明侧整段缺失）。"""
    from garmin_cn import manual

    date = args.date or dt.date.today().isoformat()
    if args.start and args.end:
        fix = manual.add_sleep(date, args.start, args.end, note=args.note or "")
        print(f"√ 已矫正 {date} 睡眠：{fix['start']} → {fix['end']}"
              f"（{fix['minutes']} 分钟）" + (f"　{fix['note']}" if fix['note'] else ""))
    else:
        fix = manual.sleep_fix_of(date)
        if not fix:
            print(f"{date} 没有手动睡眠矫正记录")
            return 0
        print(f"{date} 矫正：{fix['start']} → {fix['end']}（{fix['minutes']} 分钟）"
              + (f"　{fix['note']}" if fix.get("note") else ""))
    try:
        res = derive(Store(DB), date)
        sl = res["sleep"]
        src = res["today"].get("sleep_src")
        print(f"生效值：{sl['total_h']} h（来源 {'手动矫正' if src == 'manual' else '佳明'}）"
              f"｜近 7 天睡眠债 {sl['debt_h']} h")
    except Exception as e:
        print(f"（重算睡眠失败：{type(e).__name__}: {e}）")
    return 0


def cmd_nap(args) -> int:
    """补录日间小睡：佳明没记到（手表没进睡眠模式 / 手机还没同步）时用。"""
    from garmin_cn import manual

    date = args.date or dt.date.today().isoformat()
    if args.minutes:
        got = manual.add_nap(date, args.minutes, args.time)
        print(f"√ 已补录 {date} 小睡 {got['minutes']} 分（{len(got['items'])} 段）")

    got = manual.nap_of(date)
    if not got["minutes"]:
        print(f"{date} 没有手动小睡记录")
        return 0
    for x in got["items"]:
        print(f"  {x['time']}  {x['minutes']} 分")
    try:
        res = derive(Store(DB), date)
        g = res["today"].get("nap_min")
        sl = res["sleep"]
        print(f"佳明同日记录：{g if g else '无'}｜近 7 天睡眠债 {sl['debt_h']} h"
              f"（其中手动小睡抵扣 {sl['debt'].get('nap_manual_min') or 0} 分）")
    except Exception as e:
        print(f"（算睡眠债失败：{type(e).__name__}: {e}）")
    return 0


def cmd_status(args) -> int:
    store = Store(DB)
    st = store.stats()
    cfg = load_config()
    sess = HOME / "data" / "session.json"
    if not sess.exists():
        sess_txt = "未登录"
    else:
        from garmin_cn.net import Net

        net = Net(sess)
        sess_txt = ("已登录" if (net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))
                    else "有会话文件但缺 JWT_WEB（需重新 login）")
    print("账号：" + (cfg.get("email") or "未配置"))
    print(f"会话：{sess_txt}")
    print(f"数据：{st['days']} 天（{st['first']} ~ {st['last']}），{st['activities']} 条活动")
    print(f"上次同步：{store.get_meta('last_sync') or '从未'}")

    # 体重单独一行：它来自小米秤，同步时间线和佳明不同，混在一起会看不出「没秤」
    from garmin_cn import body as B

    bd = B.stats(store)
    if bd.get("latest"):
        when = f"{bd['stale_days']} 天前" if bd.get("stale_days") else "今天"
        bits = [f"{bd['latest']:.1f} kg（{bd['latest_date'][5:]}，{when}）"]
        for key, unit, label in (("body_fat", "%", "体脂"), ("muscle", "kg", "肌肉"),
                                 ("water", "%", "水分")):
            if bd.get(key):
                bits.append(f"{label} {bd[key]:.1f}{unit}")
        print("体重：" + " · ".join(bits))
    else:
        print("体重：暂无（run.bat body --mi 从秤拉，或 run.bat body --import <文件>）")
    print(f"数据库：{st['path']}")
    return 0


def cmd_doctor(args) -> int:
    """逐个检查前置条件，用来排查「敲了命令没反应 / 跑不通」。"""
    import importlib

    bad = 0

    def check(ok: bool, name: str, detail: str = "") -> None:
        nonlocal bad
        if not ok:
            bad += 1
        print(f" {'√' if ok else '×'} {name}" + (f"   {detail}" if detail else ""), flush=True)

    print("garmin-coach 环境自检")
    print("=" * 46)
    print(f"解释器：{sys.executable}", flush=True)
    print(f"版本  ：{sys.version.split()[0]}", flush=True)

    from garmin_cn.sysopen import is_termux

    in_termux = is_termux()
    launcher = "run.sh" if in_termux else "run.bat"
    print(f"环境  ：{'Termux（Android）' if in_termux else sys.platform}", flush=True)
    print("-" * 46, flush=True)

    exe = sys.executable.lower()
    check(sys.version_info >= (3, 11), "Python ≥ 3.11")
    check("windowsapps" not in exe, "不是 Microsoft Store 占位符",
          "" if "windowsapps" not in exe else f"→ 请用 {launcher} 启动")
    check(HOME.exists() and os.access(HOME, os.W_OK), "项目目录可写", str(HOME))

    for mod in ("net", "auth", "client", "store", "sync", "metrics", "coach", "report"):
        try:
            importlib.import_module(f"garmin_cn.{mod}")
            check(True, f"模块 garmin_cn.{mod}")
        except Exception as e:
            check(False, f"模块 garmin_cn.{mod}", f"{type(e).__name__}: {e}")

    try:
        from garmin_cn.store import Store

        st = Store(DB).stats()
        check(True, "数据库可打开",
              f"{st['days']} 天 / {st['activities']} 条活动（{st['first']} ~ {st['last']}）")
    except Exception as e:
        check(False, "数据库可打开", f"{type(e).__name__}: {e}")

    auth = GarminCNAuth(HOME)
    if auth.session_path.exists():
        check(True, "会话文件", "已保存")
    else:
        print(" · 会话文件   未登录（下一步：run.bat login）", flush=True)
    cfg = load_config()
    if cfg.get("email"):
        check(True, "已记录账号", cfg["email"])
    else:
        print(" · 已记录账号   未配置（login 时会问）", flush=True)

    from garmin_cn.net import Net

    net = Net(None, min_interval=0.2)
    for label, url, expect in (
        ("connect.garmin.cn", "https://connect.garmin.cn/modern/login", "GET"),
        ("sso.garmin.cn", "https://sso.garmin.cn/mobile/api/login?clientId=GCM_IOS_DARK", "GET"),
    ):
        try:
            status, _ = net.request(expect, url, timeout=10, retries=1)
            # 405/403 也说明域名与路由是通的，只是不接受这种探测方式
            ok = status < 500
            check(ok, f"网络 {label}", f"HTTP {status}" if ok else f"HTTP {status} 服务端异常")
        except Exception as e:
            check(False, f"网络 {label}", f"{type(e).__name__}: {e}")

    from garmin_cn import basemap as bm

    if bm.key():
        ok, msg = bm.probe()
        check(ok, "高德底图 Key", msg)
    else:
        print(" · 高德底图   未配置 Key（活动图用矢量轨迹；"
              "配了 data/amap.json 才铺真实地图）", flush=True)
    if bm.CACHE.exists():
        n = len(list(bm.CACHE.glob("*.png")))
        if n:
            check(True, "底图缓存", f"{n} 张")
    check(True, "坐标基准",
          "WGS→GCJ 纠偏" if bm.DATUM == "wgs84" else "原样（视为已是 GCJ）")

    # 小米健康云只查本地凭据，不联网（doctor 不该因为第三方接口慢半分钟）
    from garmin_cn import mi as _mi

    mc = _mi.MiCloud(HOME)
    if mc.load():
        print(f" · 小米健康云 已登录（userId {mc.user_id}，"
              f"存于 data/mi_auth.json）；`run.bat mi status` 可验证是否还有效", flush=True)
    else:
        print(" · 小米健康云 未登录（体脂秤数据源，可选）：run.bat mi login", flush=True)

    # 轨迹回放的两个依赖：底图 Key 和 ffmpeg（只影响 MP4 导出）
    from garmin_cn import basemap as _bm, video as _vid

    print(f" · 轨迹底图 {'高德 Key 已配置' if _bm.available() else '没有 Key（回放会退化成纯色底）'}"
          f"；`run.bat motion --help` 看用法", flush=True)
    _ff = _vid.ffmpeg()
    if _ff:
        print(f" · ffmpeg 已就位（{Path(_ff).name}），`{launcher} motion --mp4` 可导出视频", flush=True)
    else:
        print(" · ffmpeg 未安装：MP4 导出不可用（网页回放不受影响）；"
              "Termux 里 `pkg install ffmpeg`，Windows 把 ffmpeg.exe 放进 tools/", flush=True)

    # PNG 出图依赖无头浏览器；Termux 上注定没有，这属于预期，不是故障
    from garmin_cn.card import can_shot

    if can_shot():
        print(" · 无头浏览器 可用（一图流 / 活动图出 PNG）", flush=True)
    else:
        print(" · 无头浏览器 没找到：不出 PNG，改出 HTML 版"
              "（手机浏览器打开整张缩放，长按存图；看板与回放不受影响）", flush=True)

    # 体成分入库情况：有记录但很旧，多半是秤没再上传，不是登录问题
    from garmin_cn import body as _body

    bst = _body.stats(Store(DB))
    if bst.get("latest"):
        extra = f"，体脂 {bst['body_fat']:.1f}%" if bst.get("body_fat") else ""
        stale = bst.get("stale_days")
        print(f" · 体成分  {bst['count']} 次称重，最新 {bst['latest_date']}"
              f" {bst['latest']:.1f} kg{extra}"
              f"{f'（{stale} 天前）' if stale else ''}", flush=True)
    else:
        print(" · 体成分  0 条：run.bat body --mi（小米秤）或 run.bat body --import <文件>",
              flush=True)

    print("-" * 46)
    if bad:
        print(f"有 {bad} 项不正常，按上面提示处理。")
    else:
        print(f"全部通过，可以开始：{launcher} login")
    return 0 if not bad else 1


def cmd_key(args) -> int:
    """配置 / 检查高德底图 Key。"""
    from garmin_cn import basemap as bm

    if args.clear:
        if bm.CONF.exists():
            bm.CONF.unlink()
            print(f"已删除 {bm.CONF}")
        else:
            print("本来就没有配置文件")
        return 0

    if args.set:
        p = bm.save_key(args.set)
        print(f"已写入 {p}")
        ok, msg = bm.probe(args.set)
        print(("✓ 探活通过：" if ok else "✗ 探活失败：") + msg)
        if ok:
            print("现在跑 run.bat act --date <日期> --card 就会铺真实底图。")
            print("想重新取底图就删 data/basemaps/ 下对应的 PNG。")
        return 0 if ok else 1

    k = bm.key()
    if not k:
        print("高德底图 Key：未配置")
        print("获取步骤见 README「真实地图底图（可选，只本地）」一节；")
        print("拿到后执行：run.bat key --set 你的Key")
        return 1
    src = "环境变量 AMAP_KEY" if (os.environ.get("AMAP_KEY") or "").strip() else str(bm.CONF)
    print(f"高德底图 Key：{k[:6]}…{k[-4:]}（{len(k)} 位）")
    print(f"来源：{src}")
    if k.startswith(("sk-", "pk-", "js-")):
        print("⚠ 这看起来不是 Web 服务 Key（sk-/pk-/js- 前缀是 SDK 用的），"
              "静态地图要「Web服务」类型，形如 32 位十六进制。")
    ok, msg = bm.probe()
    print(("✓ 探活通过：" if ok else "✗ 探活失败：") + msg)
    n = len(list(bm.CACHE.glob("*.png"))) if bm.CACHE.exists() else 0
    print(f"底图缓存：{n} 张（{bm.CACHE}）")
    return 0 if ok else 1


def cmd_serve(args) -> int:
    """把 output/ 只读挂到局域网上，手机浏览器直接看。"""
    from garmin_cn.serve import run_server

    return run_server(OUT, lan=args.lan, port=args.port,
                      open_=args.open, token=args.token)


def cmd_ai(args) -> int:
    """起本机看板服务并直接打开 AI 教练页（对话 / 今日建议 / 体重 / 配置）。

    AI 接口只允许 127.0.0.1 调用，所以这里从不加 --lan：健康数据要发给模型
    服务商的事，只在本机由本机自己决定。
    """
    from garmin_cn.serve import run_server

    if args.model:
        from garmin_cn import llm

        cfg = llm.load_config()
        if args.model != "mock":
            print("× 目前 --model 只支持 mock（本地假模型，调试用）")
            return 2
        mock_port = args.mock_port
        import threading
        import sys as _sys
        _sys.path.insert(0, str(HOME / "tools"))
        import mock_llm

        srv = ThreadingHTTPServer(("127.0.0.1", mock_port), mock_llm.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        llm.save_config(mode="own", provider="custom",
                        base_url=f"http://127.0.0.1:{mock_port}/v1",
                        model="mock-1.5", api_key="mock")
        print(f"· 已切到本地假模型 http://127.0.0.1:{mock_port}/v1（仅调试，结束即恢复）")
    return run_server(OUT, lan=False, port=args.port, open_=True)


def cmd_demo(args) -> int:
    from garmin_cn.demo import build

    demo_db = HOME / "data" / "demo.db"
    if args.fresh or not demo_db.exists():
        if demo_db.exists():
            try:
                demo_db.unlink()
            except OSError:
                pass
        store = build(demo_db, days=60)
    else:
        store = Store(demo_db)
    date = dt.date.today().isoformat()
    res = derive(store, date)
    out = OUT / "demo.html"
    render(res, store, out)
    print(f"√ 演示看板：{out}")
    print(f"  数据：{store.stats()['days']} 天 / {store.stats()['activities']} 条活动")
    if args.open:
        open_any(out)
    return 0


def cmd_selftest(args) -> int:
    """不联网的自检：解析、指标、渲染全链路。"""
    from garmin_cn.sync import parse_activity, parse_hrv, parse_sleep

    assert parse_hrv({"hrvSummary": {"lastNightAvg": 62, "weeklyAvg": 55, "status": "BALANCED"}})["hrv_night"] == 62
    s = parse_sleep({"dailySleepDTO": {"sleepTimeSeconds": 27000, "deepSleepSeconds": 5400,
                                       "sleepScores": {"overall": {"value": 78}}}})
    assert s["sleep_total_min"] == 450 and s["sleep_score"] == 78, s
    a = parse_activity({"activityId": 1, "startTimeLocal": "2026-11-01 07:12:00",
                        "activityType": {"typeKey": "running"}, "duration": 2400, "distance": 8000})
    assert a["pace_s_km"] == 300, a

    # 小米健康云的加密外壳：RC4-drop1024 + 双签名，改坏了在这里先炸
    from garmin_cn import mi as MI

    sn = MI._signed_nonce(b"s" * 16, MI._nonce())
    data = '{"key":"weight"}'
    h1 = MI._sign("POST", "/p", {"data": data}, sn)
    assert MI._sign("POST", "/p", {"data": data}, sn) == h1
    assert MI.rc4_drop1024(sn, MI.rc4_drop1024(sn, data.encode())) == data.encode()
    assert MI._sign("POST", "/p", {"data": data, "rc4_hash__": h1}, sn) != h1
    mi_rec = MI.to_records([{"time": 1767225600, "zone_offset": 28800,
                             "value": '{"weight":68.4,"muscle_rate":42.1}'}])[0]
    assert mi_rec["weight_kg"] == 68.4 and mi_rec["muscle_kg"] == 28.8, mi_rec
    # 安全验证（短信）用的小工具：context 提取、带/不带前缀的 JSON、中文错误文案
    assert MI._ctx_of("https://a/b?sid=x&context=ABC123&_locale=zh") == "ABC123"
    assert MI._json_of('&&&START&&&{"code":0}')["code"] == 0
    assert MI._json_of('{"code":0}')["code"] == 0
    assert "图形验证码" in MI._verify_error({"code": 87001})
    assert MI._cookie_parse("a=1; b=2")["b"] == "2"
    try:
        MI._json_of("<html>登录页</html>")
        raise AssertionError("非 JSON 应当报错")
    except MI.MiAuthError:
        pass

    import tempfile

    tmp_db = Path(tempfile.gettempdir()) / "garmin_selftest.db"
    tmp_html = Path(tempfile.gettempdir()) / "garmin_selftest.html"
    if tmp_db.exists():
        tmp_db.unlink()
    store = Store(tmp_db)
    from garmin_cn.demo import build as build_demo

    build_demo(tmp_db, days=40, seed=3)
    # demo 数据默认没有体重：补两条进去，否则营养那段断言会被静默跳过
    from garmin_cn import body as BD

    today = dt.date.today()
    BD.save(store, [
        {"date": (today - dt.timedelta(days=10)).isoformat(), "ts": "08:00",
         "weight_kg": 67.0, "body_fat_pct": 16.4, "muscle_kg": 52.0,
         "water_pct": 61.8, "visceral": 3, "bmr": 1560},
        {"date": today.isoformat(), "ts": "08:00",
         "weight_kg": 66.2, "body_fat_pct": 15.9, "muscle_kg": 52.1,
         "water_pct": 62.2, "visceral": 3, "bmr": 1560},
    ], "mi")
    res = derive(store, dt.date.today().isoformat())
    assert res["readiness"]["score"] is not None
    assert res["load"]["ctl"] >= 0

    # 手机端出图画幅契约：成品必须 9:16，且 HTML 带尺寸探针。
    # 少了任一项，App 导出的长图就会被系统分享面板二次裁切。
    from garmin_cn import actcard as ACTCARD
    from garmin_cn import card as CARD
    from garmin_cn import exportspec as EX
    EX.check()
    for bg in (EX.BG_CARD, EX.BG_ACT):
        sp = EX.spec(bg)
        assert (sp["export_width"], sp["export_height"]) == (1080, 1920), sp
        assert sp["export_width"] * 16 == sp["export_height"] * 9, sp
        assert sp["export_mode"] == "contain", sp
    for mod in (CARD, ACTCARD):
        assert mod._EXPORT_PROBE == EX.probe()
        assert EX.CANVAS_ATTR in mod._EXPORT_PROBE
    for h in (CARD.build_html(res),
              ACTCARD.build_html({}, {}, None, None, "自检")):
        assert EX.CANVAS_ATTR in h and "__gcExportInfo" in h, "HTML 缺导出探针"
        assert h.count("__gcExportInfo") == 1, "探针重复注入"
        assert "__gcFit" in h and "visualViewport" in h, "HTML 缺缩放校正探针"

    # 间歇训练（快慢交替）自动切底部双栏：左快组、右慢组；
    # 匀速跑 / 单调加速不能误报，非间歇活动的分圈表必须原样保留。
    from garmin_cn import interval as IV

    IV.check()
    iv_pts: list[dict] = []
    t, dist = 0.0, 0.0
    for _ in range(6):
        for spd, secs in ((4.2, 95.0), (2.6, 77.0)):
            for _ in range(int(secs / 5)):
                iv_pts.append({"t": t, "spd": spd, "hr": 165, "cad": 180,
                               "dist": dist})
                t += 5.0
                dist += spd * 5.0
    iv_html = ACTCARD.build_html({}, {"series": {"points": iv_pts}},
                                 None, None, "间歇自检")
    assert 'class="iv"' in iv_html and "快组" in iv_html
    assert "慢组 / 恢复" in iv_html and "间歇结构" in iv_html
    assert '<div class="lhead">' not in iv_html, "间歇版不该再渲染原分圈表"
    assert 'class="iv"' not in ACTCARD.build_html({}, {}, None, None, "自检")

    # 原地休息型间歇（真实 15×400 m 样本形态）：静止点不能被过滤掉，
    # 右栏要写「平均恢复 1:xx」而不是几十分钟每公里的假配速，15 组逐组列全。
    rest_pts: list[dict] = []
    t = 0.0
    for _ in range(15):
        for spd, secs in ((4.6, 86.0), (0.0, 90.0)):
            for _ in range(int(secs / 4)):
                rest_pts.append({"t": t, "spd": spd, "hr": 160, "cad": 200,
                                 "dist": 0.0})
                t += 4.0
    rest_html = ACTCARD.build_html({}, {"series": {"points": rest_pts}},
                                   None, None, "间歇自检（原地恢复）")
    assert 'class="iv"' in rest_html and "平均恢复" in rest_html
    assert "组间恢复" in rest_html and "速度差" not in rest_html
    assert 'class="ivmore"' not in rest_html, "15 组应逐组列全"
    assert rest_html.count('class="ivrow"') == 30, "左右各 15 行"

    # 手机端 WebView 的缩放自校正：density=3 时布局视口 1080 CSS px 只露出
    # 360 CSS px，必须把画布缩到 1/3；归一后内容高度与 density 无关。
    # 少了这一步，draw(Canvas) 只截到左上角，分享预览会「只剩一个角」。
    assert abs(EX.fit_scale(1080, 360) - 1 / 3) < 1e-9
    assert EX.fit_scale(1080, 1080) == 1.0
    assert EX.fit_scale(1080, 0) == 1.0
    assert EX.content_height(1080, 1080, 2250) == 2250
    assert EX.content_height(1080, 1080, 1920) == 1920

    # 历史数据（手机端「拉取更多 / 查询历史」）：接口面、区间门禁与活动分页。
    # 只跑不联网的分支——合法日期的 sync_range 会真发请求，不能进自检。
    import json as _json
    import tempfile as _tf
    from pathlib import Path as _Path
    from garmin_cn import appapi as AP
    from garmin_cn.store import Store as _Store
    from garmin_cn.sync import SyncService as _Sync

    assert {"sync_range", "activities", "days_index"} <= AP._API

    def bad_range(*a):
        return _json.loads(AP.call("sync_range", _json.dumps(list(a))))

    assert bad_range("2026-11-06", "2026-11-01")["ok"] is False   # 起止颠倒
    assert bad_range("2020-01-01", "2026-11-06")["ok"] is False   # 超过 3 年
    assert bad_range("not-a-date")["ok"] is False                 # 非法日期
    bad_q = _json.loads(AP.call("activities", _json.dumps(["bad-date"])))
    assert bad_q["ok"] is False and "YYYY-MM-DD" in bad_q["error"]
    d = _json.loads(AP.call("days_index", _json.dumps([1])))
    assert d["ok"] and all(k in d for k in ("first", "last", "count", "items"))
    q = _json.loads(AP.call("activities", _json.dumps([None, None, None, 5, 0])))
    assert q["ok"] and all(k in q for k in ("items", "total", "has_more", "offset"))

    # 网页外壳与交互树：四个 tab 共用一套 ui.page，卡片动作走 data-act，
    # 活动行走 data-aid。约束见 docs/交互树.md：一个动作只有一个入口。
    from garmin_cn import history as HW
    from garmin_cn import ui as U

    idx = HW.index_section(U.coverage())
    assert "/view?date=" in idx and "日汇总覆盖" in idx
    hp = HW.history_page({"type": ["run"]})
    assert "page-head" in hp and 'name="start"' in hp
    assert "data-aid=" in hp and "GC.activity" in hp
    # 「补拉历史」只在历史页出现一次，今日/我的都不再重复
    assert hp.count('data-act="historySync"') == 1
    home = U.home_page()
    assert "page-head" in home and 'data-act="export"' in home and "data-aid=" in home
    assert 'href="/history"' not in home, "首页不该再给历史入口，底部 tab 已经有了"
    me = U.me_page()
    assert "page-head" in me and 'data-act="login:garmin"' in me
    assert 'data-act="login:coros"' in me, "数据源行要直达对应登录"
    assert 'data-act="historySync"' not in me and 'data-act="settings"' not in me
    assert 'href="/files"' in me
    assert 'data-act="amap"' in me and "地图底图" in me, "我的页要有底图 Key 入口"
    # 看板导航只剩「回来源 tab + 前后翻日期」，不再有横向 tab 入口
    nav = U.nav_html("2026-10-21",
                     {"first": "2026-10-01", "last": "2026-11-06"})
    assert "gc-nav" in nav
    assert "/view?date=2026-10-20" in nav and "/view?date=2026-10-22" in nav
    assert 'href="/history"' in nav and 'href="/"' not in nav
    # 同页参数变化走 replace，不往历史栈堆条目
    assert "data-nopush" in nav and "data-nopush" in U.BRIDGE_JS
    assert "max-width:820px" in U.DASH_FIX and "max-width:820px" in U.AI_FIX
    # 训练计划页：读到/读不到都要能出页面，且能回今日、有刷新入口
    pp = U.plan_page({})
    assert "PLAN · 训练计划" in pp
    assert 'class="back" href="/"' in pp
    assert "planRefresh" in pp
    assert ('class="plan-hero"' in pp) or ("还没读到训练计划" in pp)

    # 训练计划：日历优先。workout/race 归一化成同一结构，activity 只用来打
    # 「已完成」标记，note 等其它日历项不能当成课。
    from garmin_cn import plan as PL

    cal_items = [
        {"itemType": "workout", "date": "2026-11-03", "title": "轻松跑",
         "duration": 3000, "distance": 8000, "sportTypeKey": "running"},
        {"itemType": "activity", "date": "2026-11-03", "title": "跑步"},
        {"itemType": "workout", "date": "2026-11-07", "title": "节奏跑",
         "duration": 2400, "distance": 7000},
        {"itemType": "race", "date": "2026-11-15", "title": "半马",
         "distance": 21097},
        {"itemType": "note", "date": "2026-11-09", "title": "备注"},
    ]
    cal = PL.from_calendar(cal_items, "2026-11-07")
    assert cal["source"] == "calendar" and cal["scheduled"], cal
    assert cal["today"]["items"][0]["minutes"] == 40
    assert cal["today"]["items"][0]["km"] == 7.0
    assert cal["week"][1]["done"] is True, "周二有 activity，应标已完成"
    assert cal["next_date"] == "2026-11-15", cal["next_date"]  # 11-09 是 note，跳过
    assert PL.from_calendar([], "2026-11-07")["scheduled"] is False
    # 未来未执行的 activity（没有 activityId）要当排课，不能当成已完成
    fut = PL.from_calendar([
        {"itemType": "activity", "date": "2026-11-08", "title": "明天的课",
         "duration": 3600, "distance": 10000},
        {"itemType": "workout", "date": "2026-11-05", "title": "周四的课"},
        {"itemType": "activity", "date": "2026-11-05", "title": "跑步",
         "activityId": 999},
    ], "2026-11-07")
    assert fut["week"][6]["items"], "未来 activity 应算课"
    assert fut["week"][3]["done"] is True, "带 activityId 的才算已完成"
    assert PL._months("2026-11-07")[0] == (2026, 10), "要连上个月一起拉，兼容月份语义"

    # 一图流读计划快照必须走 GARMIN_HOME：APK 里代码在只读 assets、数据在
    # filesDir，用 __file__ 推路径永远读不到——手机上一图流没计划就是这个坑。
    import os as _os
    import tempfile as _tf2

    with _tf2.TemporaryDirectory() as td:
        _data = Path(td) / "data"
        _data.mkdir(parents=True, exist_ok=True)
        (_data / "plan.json").write_text(
            _json.dumps(cal, ensure_ascii=False), encoding="utf-8")
        _old = _os.environ.get("GARMIN_HOME")
        _os.environ["GARMIN_HOME"] = td
        try:
            _got = PL.attach({}).get("plan") or {}
            assert _got.get("name") == "佳明日历", \
                "attach() 要按 GARMIN_HOME 读快照，不能用 __file__ 推"
            from garmin_cn.card import _week_cells

            _rows = _week_cells(_got, "2026-11-07")
            assert _rows.count('class="wkd') == 7, "一图流周视图应横排 7 格"
            assert '<div class="wk">' in _rows, "横排容器必须存在，不能退回竖排 7 行"
        finally:
            if _old is None:
                _os.environ.pop("GARMIN_HOME", None)
            else:
                _os.environ["GARMIN_HOME"] = _old
    # 回放导出（手机端）：页面走 http，底图/照片必须是同源 URL。
    # 真机绝对路径是 /data/user/0/<包名>/files/data/basemaps/x.png，
    # 服务端白名单只认 /data/basemaps/x.png —— 不改写就 404，页面 ready
    # 永远 false，导出静默停在 0 帧（这是踩过的坑，别再改回去）。
    from garmin_cn import basemap as BM
    from garmin_cn import motion as MO

    _web = MO._web_url(str(BM.ROOT / "data" / "basemaps" / "_selftest.png"))
    assert _web == "/data/basemaps/_selftest.png", _web
    # 已经是同源路径的不再加工；私密文件（db/session）不转
    assert MO._web_url("/data/basemaps/_selftest.png") == "/data/basemaps/_selftest.png"
    assert MO._web_url(str(BM.ROOT / "data" / "session.json")) != "/data/session.json"
    _msrc = Path(MO.__file__).read_text(encoding="utf-8")
    # toURL 不能给已带前导 / 的路径再补一个（会变成 //data/... 协议相对 URL，
    # 浏览器会去找主机名为 data 的地址，请求到不了本机服务）
    assert "if (n.startsWith('/')) return n;" in _msrc, "toURL 的前导斜杠判断丢了"
    assert "{ method: 'POST' }" in _msrc, "__done 必须用 POST 写完成标记"
    assert "window.__motion.ready = !!baseBaked;" in _msrc, "ready 只该要求底图"
    assert HW._date({"date": ["not-a-date"]}, "date", "2026-11-06") == "2026-11-06"
    # 底图 Key 校验：太短的串直接拒，且不写盘
    _amap = _json.loads(AP.call("set_amap_key", _json.dumps(["short"])))
    assert not _amap.get("ok") and "不完整" in (_amap.get("error") or "")

    # ---- 素材裁剪（原生侧）：预览必须与结果一致 ----
    # 裁剪界面（ClipTrimDialog）和剪辑器（ClipTrimmer）是配对的：
    #   起点必须落在关键帧上，否则从 GOP 中间切过去开头花屏；
    #   两条轨必须共用同一个时间原点，否则音画差零点几秒就错位。
    # 这两条都是「改了看不出来、但结果就是不对」的坑，钉在自检里。
    _kt = HOME / "android" / "app" / "src" / "main" / "java" / "com" / "garmincoach" / "app"
    _trim_src = (_kt / "ClipTrimmer.kt").read_text(encoding="utf-8")
    _dlg_src = (_kt / "ClipTrimDialog.kt").read_text(encoding="utf-8")
    # 剪辑器：视频轨用 PREVIOUS_SYNC 找起点（对齐到关键帧）
    assert "MediaExtractor.SEEK_TO_PREVIOUS_SYNC" in _trim_src, "起点没吸附关键帧"
    # 时间原点统一：baseUs 算一次，所有轨共用，不能每条轨各算各的。
    # 早期版本音频单独 seek 到 startMs，视频对齐到前一个关键帧，
    # 两者差零点几秒 → 音画错位（不修就是「剪出来声音和画对不上」）。
    assert "val baseUs =" in _trim_src, "缺少统一时间原点 baseUs"
    # 轨道循环里的那次 seek（最后一次）必须用 baseUs，不能各轨各算
    _seek_blk = _trim_src[_trim_src.rindex("ex.seekTo("):][:200]
    assert "baseUs," in _seek_blk, "轨道 seek 没统一用 baseUs（音画会错位）"
    # 起点之前的样本要丢掉，否则减去 baseUs 会得到负时间戳
    assert "if (pts >= baseUs)" in _trim_src, "没丢弃原点之前的样本"
    # 裁剪界面：起点吸附关键帧、终点自由 —— 两端语义不同，不能一视同仁
    assert "SAMPLE_FLAG_SYNC" in _dlg_src, "关键帧扫描没了"
    assert "getScaledFrameAtTime" in _dlg_src, "胶片条缩略图没了"
    # 吸附的判据不能被改成永假（那样等于没吸附，起点会落到 GOP 中间 → 开头花屏）
    assert "if (!snapEnabled || keys.isEmpty()) return t" in _dlg_src, \
        "起点没吸附关键帧（snap 的短路条件被改了）"
    # 预览要有画面，不能又退回裸滑块
    assert "TextureView" in _dlg_src and "MediaPlayer" in _dlg_src, "预览播放器没了"
    # 排除注释后再查 SeekBar：注释里提到「之前的裸 SeekBar」是正常的
    _dlg_code = "\n".join(
        ln for ln in _dlg_src.splitlines() if not ln.strip().startswith(("*", "//", "/*")))
    assert "SeekBar" not in _dlg_code, "不该再用裸 SeekBar（用户看不到画面）"

    # ---- 素材（图片 / 视频）插入链路 ----
    # 这条链上有三个坑，任一没填上，表现都是「加了素材，回放里什么都没有」：
    #   1) 合成轨迹的 t0 单位；2) 素材没带拍摄时间；3) 图片根本没被渲染。
    _ap_src = Path(AP.__file__).read_text(encoding="utf-8")
    # photo.match 用 (t_utc_ms - t0) / 1000 算相对秒，t0 给秒就全被判成「不在活动时段」
    assert '"t0": int(t0 * 1000)' in _ap_src, "合成轨迹的 t0 必须是毫秒"
    assert "def _probe_material" in _ap_src, "素材没读拍摄时间/坐标"
    assert '"kind": kind' in _ap_src, "素材没带类型（图片/视频）"
    assert AP._kind_of(Path("a.PNG")) == "image", "图片扩展名识别"
    assert AP._kind_of(Path("a.mp4")) == "video", "视频扩展名识别"
    # 没有时间信息的素材必须均匀铺开，不能整批丢掉
    from garmin_cn import motion as MOT

    _rt = {"n": 2, "t0": 1_700_000_000_000, "t": [0, 200]}
    _cl = [{"name": "a", "t_utc": None}, {"name": "b", "t_utc": None}]
    MOT._spread_clips(_cl, _rt)
    assert _cl[0]["t_utc"] == 1_700_000_066_666, _cl[0]
    assert _cl[1]["t_utc"] == 1_700_000_133_333, _cl[1]
    assert _cl[0]["approx"] and _cl[1]["approx"], "铺开的素材要标 approx"
    # 已经有了拍摄时间的不动
    _cl2 = [{"name": "c", "t_utc": 1_700_000_050_000}]
    MOT._spread_clips(_cl2, _rt)
    assert _cl2[0]["t_utc"] == 1_700_000_050_000, "有时间信息的素材不该被改"
    _mo_src = Path(MOT.__file__).read_text(encoding="utf-8")
    assert "clips = _spread_clips(clips, rt)" in _mo_src, "素材没走均匀铺开兜底"
    assert "if (p.clip.kind === 'image')" in _mo_src, "页面没处理图片素材"
    assert "need.push(p.clip.src)" in _mo_src, "图片素材没进图片预加载队列"
    # ---- 总时长：30 秒巡航 + 素材停留。停留是**叠加**的，不许挤占巡航 ----
    # 旧版把素材停留塞进 30 秒预算里：素材一多，轨迹就被压成几秒飞完全程；
    # App 又按 30 秒算帧数，末尾的素材直接被截掉——看着就像「锚点错了」。
    _hold3 = [{"t": 1.0, "clip": {"dur": 8.0}}, {"t": 2.0, "clip": {"dur": 3.0}},
              {"t": 3.0, "clip": {"dur": 3.0}}]
    assert MOT._timeline_total(30.0, _hold3) == 44.0, MOT._timeline_total(30.0, _hold3)
    assert MOT._timeline_total(30.0, []) == 30.0, "没有素材时总长应该正好是巡航预算"
    # 长素材也要整段放完（60 秒的老上限会把长视频截掉）
    assert MOT._timeline_total(30.0, [{"t": 1.0, "clip": {"dur": 90.0}}]) == 120.0
    # 页面收片尾要靠算出来的真实总时长；帧数也必须按它算
    assert "const TOTAL = TL.total;" in _mo_src, "页面没用真实总时长"
    assert "duration: TOTAL" in _mo_src, "导出接口没暴露真实总时长"
    assert "if (playT >= D.play)" not in _mo_src, "片尾判断还在用旧的 30 秒预算"
    assert "if (p >= D.play) return D.dur" not in _mo_src, "轨迹取值还在用旧的 30 秒预算"
    assert "int(round(total * fps))" in _ap_src, "帧数没按真实总时长算（末尾素材会被截掉）"
    # 桌面导出用的收帧服务：`/__done` 和 `/__diag` 没 `i` 参数，掉进收帧分支
    # 就会被当成 i=0 覆盖真首帧（`/__done` 的 body 为空 → 首帧 0 字节，成片少一帧）
    from garmin_cn import video as _vid

    _vsrc = Path(_vid.__file__).read_text(encoding="utf-8")
    assert 'if self.path.startswith(("/__done", "/__diag"))' in _vsrc, \
        "收帧服务没显式吃掉 /__done 与 /__diag，首帧会被写成 0 字节"
    # ---- 音轨：素材原声要按时间轴混进成片 ----
    from unittest import mock as _mock

    # 三个坑：① 页面给 WebView 的 src 是 /data/... URL，解码器打不开，
    #   必须另存一份磁盘路径；② 图片段 dur=0 不能混音；③ 时间轴是页面 POST
    #   上来的，路径必须先过白名单，不能让它指向任意文件。
    assert '"local": str(Path(src).resolve())' in _mo_src, "payload 没保留素材磁盘路径"
    assert "w.clip.local || w.clip.src" in _mo_src, "页面没把本地路径回传给混音"
    _web_blk = _mo_src[_mo_src.index("    if web:"):][:400]
    assert "local" not in _web_blk, "web 改写不该动 local（原生要磁盘路径）"
    assert 'w.get("file") or w.get("src")' in _vsrc, "桌面 ffmpeg 没优先用本地路径"
    assert "def motion_audio(" in _ap_src, "原生拿不到混音清单"
    assert '"motion_audio"' in _ap_src, "motion_audio 没注册进 App 接口白名单"
    # 真机素材路径 /data/user/0/<包名>/files/data/media/... 也以 /data/ 开头，
    # 必须先按「确实存在的绝对路径」认，否则会被当成同源 URL 拼错、原声全丢
    assert "if Path(s).is_absolute() and Path(s).is_file():" in _ap_src, \
        "真机绝对路径会被误判成同源 URL"
    with _tf.TemporaryDirectory() as _atd:
        _aroot = _Path(_atd)
        (_aroot / "data/media").mkdir(parents=True)
        (_aroot / "data/motion_cache").mkdir(parents=True)
        _av = _aroot / "data/media/a.mp4"
        _av.write_bytes(b"x")
        _bv = _aroot / "data/motion_cache/b.mp4"
        _bv.write_bytes(b"x")
        (_aroot / "data/garmin.db").write_bytes(b"x")
        _arun = "audio123"
        _adir = _aroot / "output/_frames" / _arun
        _adir.mkdir(parents=True)
        _tl = _adir / "_timeline.json"
        _tl.write_text(_json.dumps([
            {"file": "/data/media/a.mp4", "src": "/data/media/a.mp4",
             "a": 3.0, "lead": 0.5, "dur": 8.0},
            {"file": "", "src": "", "a": 12.0, "lead": 0, "dur": 0},   # 图片段
            {"file": "/data/media/gone.mp4", "a": 15.0, "dur": 4.0},   # 文件丢了
            {"file": "/data/garmin.db", "a": 16.0, "dur": 4.0},        # 越权
            {"src": "/data/motion_cache/b.mp4", "a": 1.0, "dur": 5.0},  # 老页面只有 src
            {"file": str(_bv), "src": "/data/media/wrong.mp4",
             "a": 20.0, "dur": 2.0},                          # file 必须压过 src
        ]), encoding="utf-8")
        with _mock.patch.object(AP, "HOME", _aroot), \
                _mock.patch.object(AP, "OUT", _aroot / "output"):
            assert AP._audio_local("/media/a.mp4") == _av
            assert AP._audio_local(str(_bv)) == _bv
            assert AP._audio_local("/data/../etc/passwd") is None, "不能跳出 data/"
            assert AP._audio_local("/data/garmin.db") is None, "不能读私有文件"
            assert AP._audio_local("/data/media/a.jpg") is None, "图片没有音轨"
            _got = AP.motion_audio(_arun)
            assert AP.motion_audio("no-such-run")["items"] == [], "没有时间轴应返回空清单"
        assert _got["ok"] and _got["total"] == 3, _got
        assert _got["items"][0]["at"] == 1.0, "老页面只给 src 时没按起点排序"
        assert _got["items"][1]["at"] == 3.5, _got["items"]
        assert _got["items"][1]["file"] == str(_av), "URL 没映射回本地文件"
        assert _got["items"][2]["file"] == str(_bv), "file 没压过 src"
        assert not AP.motion_audio("bad run!")["ok"], "run 没做校验"
        # 桌面 ffmpeg 读的是同一份时间轴，优先 file、丢掉图片段
        _tl2 = _adir / "_desktop_timeline.json"
        _tl2.write_text(_json.dumps([
            {"file": str(_av), "src": "/data/media/a.mp4",
             "a": 2.0, "lead": 0.5, "dur": 8.0},
            {"src": "/data/media/a.mp4", "a": 4.0, "dur": 0},
        ]), encoding="utf-8")
        _ca = _vid._clip_audio(_tl2)
        assert len(_ca) == 1 and _ca[0][0] == str(_av) and _ca[0][1] == 2.5, _ca
    # 手机端：解码混音 → AAC → 与视频交织写进同一个 MP4
    _kt_dir = Path(AP.__file__).parent.parent / "android/app/src/main/java/com/garmincoach/app"
    if (_kt_dir / "MotionAudio.kt").is_file():
        _ma = (_kt_dir / "MotionAudio.kt").read_text(encoding="utf-8")
        _me = (_kt_dir / "MotionEncoder.kt").read_text(encoding="utf-8")
        _main = (_kt_dir / "MainActivity.kt").read_text(encoding="utf-8")
        assert "createDecoderByType" in _ma, "没解码素材音轨"
        assert "MIMETYPE_AUDIO_AAC" in _ma, "没压成 AAC"
        assert "muxer.addTrack(it.format)" in _me, "音轨没在 muxer.start() 前加进 MP4"
        assert "writeAudio(info.presentationTimeUs, false)" in _me, "音视频没交织"
        assert 'apiCall("motion_audio", run)' in _main, "导出没取混音清单"
    # ---- 导出提速 + 顶部百分比 ------------------------------------------
    # 旧版「720 帧全部写盘 → 才开始编码」两段串行；现在边收边编，
    # 编码器会盯着 f_XXXXX.jpg 出现就立刻读，因此收帧必须原子落盘。
    _sv_src = _Path(_vid.__file__).parent.joinpath("serve.py").read_text("utf-8")
    assert "os.replace(tmp, target)" in _sv_src, "App 收帧没原子改名，会读到半张 JPEG"
    assert 'f".f_{i:05d}.tmp"' in _sv_src, "临时帧文件名会匹配 f_*.jpg，进度被算多"
    assert "os.replace(tmp, target)" in _vsrc, "桌面导出收帧没同步原子写入"
    # 真起一次本机服务，验证「文件出现即完整、临时文件不计入进度」
    with _tf.TemporaryDirectory() as _htd:
        from functools import partial as _fpartial
        from http.server import ThreadingHTTPServer as _THS
        from garmin_cn import serve as _sv
        import threading as _th
        import urllib.request as _ur
        _root = _Path(_htd)

        class _Quiet(_sv._Handler):
            def log_message(self, *a):
                pass

        _srv = _THS(("127.0.0.1", 0),
                    _fpartial(_Quiet, directory=str(_root)))
        _th.Thread(target=_srv.serve_forever, daemon=True).start()
        # 盯着 os.replace：只用字符串断言的话，实现改回「直接 write_bytes」
        # 也可能蒙混过关（文件照样是完整的），必须验证确实走了原子改名。
        _orig_replace, _renames = _sv.os.replace, []

        def _spy_replace(src, dst):
            _renames.append((str(src), str(dst)))
            return _orig_replace(src, dst)

        _sv.os.replace = _spy_replace
        try:
            _payload = b"\xff\xd8FAKEJPEG" * 256
            _url = ("http://127.0.0.1:%d/__frame?i=7&run=benchrun1"
                    % _srv.server_address[1])
            with _ur.urlopen(_ur.Request(_url, data=_payload, method="POST"),
                             timeout=5) as _resp:
                assert _resp.status == 204
            _fr = _root / "_frames" / "benchrun1"
            assert (_fr / "f_00007.jpg").read_bytes() == _payload, \
                "编码器可能读到写了一半的帧"
            assert len(_renames) == 1 and _renames[0][0].endswith(".tmp"), \
                "收帧没走「临时文件 → 原子改名」"
            assert not list(_fr.glob(".*.tmp")), "原子改名后临时文件没消失"
            assert len(list(_fr.glob("f_*.jpg"))) == 1, "临时文件被算进帧进度"
        finally:
            _sv.os.replace = _orig_replace
            _srv.shutdown()
            _srv.server_close()
    # 编码器：流式帧源 / GPU Surface 主路径 / CPU 自动降级 / 输入缓冲重试
    _me = (_kt_dir / "MotionEncoder.kt").read_text("utf-8")
    assert "fun interface FrameAt" in _me and "source.get(i)" in _me, \
        "编码没有惰性帧源，做不到边收边编"
    assert "val scratch = if (!useSurface) Scratch(width, height) else null" in _me, \
        "每帧还在重新分配 8MB 像素缓冲，或 CPU 降级路径没了"
    assert "YR[(c shr 16) and 255]" in _me and "yb.put(s.y, 0, w)" in _me, \
        "CPU 降级的 YUV 没改查表 + 按行批量 put"
    assert "val y0 = yb.position()" in _me, \
        "plane buffer 有 crop offset 时会整幅错行"
    assert "while (idx < 0 && spin++ < 200" in _me, \
        "输入缓冲拿不到时会丢帧（pts 不推进 → 成片变短）"
    assert "bmp.recycle()\n                    continue" not in _me, \
        "还留着丢帧的 continue"
    assert "onProgress(i + 1)" in _me, "编码侧没上报已编码帧数"
    assert "70 + (i + 1) * 30" not in _me, \
        "还在用「70~100」的串行阶段百分比，并行时会假跳到 70%"
    # GPU 路径：Surface 输入 + 自动降级
    assert "codec.createInputSurface()" in _me, "没用 MediaCodec 输入 Surface"
    assert "codec.signalEndOfInputStream()" in _me, "Surface 输入没送 EOS"
    assert "for (useSurface in listOf(true, false))" in _me, \
        "GPU 初始化失败时没有自动重建 codec 降级 CPU"
    assert "COLOR_FormatSurface" in _me and "COLOR_FormatYUV420Flexible" in _me, \
        "两种输入格式的 color format 没分别设置"
    assert "val audio = try" in _me and "MotionAudio.build(segments, totalUs)" in _me, \
        "GPU→CPU 降级会把音轨解码重做一遍或整段丢掉"
    assert "val via = if (useSurface)" in _me and "mux.audioSegments, via" in _me, \
        "结果里没标出走的是 GPU 还是 CPU"
    assert "opts.inBitmap = it" in _me and "inMutable = true" in _me, \
        "每帧还在重新分配 8MB 解码缓冲，720 帧会有 5GB+ 的 GC 压力"
    assert "reusable?.recycle()" in _me, "复用的 Bitmap 最终没释放"
    assert "GLUtils.texSubImage2D" in _me or "gl!!.draw(bmp, pts)" in _me, \
        "GPU 路径没有逐帧提交画面"
    _gl = (_kt_dir / "MotionGL.kt").read_text("utf-8")
    assert "eglPresentationTimeANDROID(display, eglSurface, ptsUs * 1000L)" in _gl, \
        "Surface 输入没按微秒→纳秒设帧时间戳，成片时长会错"
    assert "GLUtils.texSubImage2D" in _gl and "GLUtils.texImage2D" in _gl, \
        "纹理没分配/复用，每帧都在重新申请 GPU 内存"
    assert "EGL14.eglSwapBuffers" in _gl, "没把帧提交给编码器"
    assert "1.0 - aTex.y" in _gl, "Bitmap 上传后画面会上下颠倒"
    assert "EGL_RECORDABLE_ANDROID" in _gl and "listOf(true, false)" in _gl, \
        "不支持 recordable 扩展的设备会直接初始化失败"
    assert "eglGetError()" in _gl or "glGetError()" in _gl, \
        "GL/EGL 出错没有检查，会静默出花屏"
    # App：全程统一百分比、不显示帧数，且渲染与编码并行
    _main = (_kt_dir / "MainActivity.kt").read_text("utf-8")
    assert 'subtitle.text = "生成回放 $show%"' in _main, "顶部没改成百分比"
    assert '生成回放 $n/$frames 帧' not in _main, "顶部还在显示帧数"
    assert '生成回放 $v%' not in _main, "还留着旧的百分比变量"
    assert '"编码中…"' not in _main and '"混入原声并编码中…"' not in _main, \
        "还有阶段性文字覆盖百分比"
    assert 'subtitle.text = "生成回放 0%"' in _main, "准备阶段没接上百分比"
    assert "renderDone.set(n)" in _main and "encodeDone.set(done)" in _main, \
        "渲染/编码没有各自上报帧数"
    assert "minOf(renderDone.get(), encodeDone.get())" in _main, \
        "进度没取两阶段的短板，并行时会虚高"
    assert "report(min(70," not in _main and "70L /" not in _main, \
        "还留着 0~70 / 70~100 的固定阶段分段"
    assert "report(100)" in _main, "编码收尾没有强制到 100%"
    assert "frameCount = total" in _main and "frameAt = MotionEncoder.FrameAt" in _main, \
        "没把总帧数与惰性帧源交给编码器"
    assert "cancelled = { cancel.get() }" in _main and "onProgress = report" in _main, \
        "取消信号/进度回调没接上"
    assert "private fun awaitFrame" in _main and \
        "MOTION_STALL_MS) return null" in _main, \
        "等帧没有超时，导出卡住会无限空转"
    assert 'File(dir, "_timeline.json").isFile' in _main and "f_00000.jpg" in _main, \
        "没等时间轴与首帧就取音频清单，会漏掉原声"
    assert "import java.util.concurrent.atomic.AtomicBoolean" in _main, \
        "取消信号不是线程安全的"
    assert "import java.util.concurrent.atomic.AtomicInteger" in _main, \
        "双线程帧计数不是线程安全的"
    assert "encoded.set(true)" in _main and "ui { busy(false); updateSubtitle() }" in _main, \
        "超时/失败时进度圈可能一直转"
    assert "motion render done" in _main and "motion export done" in _main, \
        "没有分项计时，真机上定位不了瓶颈"
    assert "${r.via}" in _main and "startedAt" in _main, \
        "完成提示没说明走 GPU/CPU 与总耗时"
    # ---- v44：超时只判「卡死」，不判「慢」 ----------------------------------
    # 旧版从点击起 12 分钟硬 deadline：真机一帧几秒时进度还在走，到点却直接
    # cancel 并弹「回放生成超时，请重试」；用户重试又是同样的 12 分钟，永远
    # 出不了片。等帧 120 秒 / 首帧 60 秒 / 30 秒 diag 也是同一类误杀。
    assert "12 * 60 * 1000" not in _main, "还有按总时长一刀切的超时"
    assert 'toast("回放生成超时，请重试")' not in _main, \
        "还在把「慢」报成笼统的超时，让用户白重试"
    assert "120_000" not in _main and "for (k in 0 until 300)" not in _main, \
        "还留着等帧 120 秒 / 首帧 60 秒的固定死线"
    assert "MOTION_STALL_MS" in _main and "MOTION_FIRST_FRAME_MS" in _main, \
        "没有区分「首帧宽限」与「无进展卡死」"
    assert "lastProgressAt.set(System.currentTimeMillis())" in _main, \
        "没有共享心跳，帧还在推进也可能被判超时"
    assert "MOTION_STALL_MS) return null" in _main, \
        "等帧超时没跟全局心跳挂钩，慢机器会被掐断"
    assert "if (ok) report(100)" in _main, "超时/失败后还会把进度假推到 100%"
    assert "motion stalled" in _main, "卡死时没有日志，真机定位不了瓶颈"
    assert 'diag.length() > 0' in _main, \
        "诊断还是按固定 30 秒判，或没处理 diag 为空对象的情况"
    # 桌面补渲同理：整轮固定 max(120, 缺帧数*0.5) 秒会把正在推进的 Chrome 掐断
    assert "deadline = time.time() + max(120, len(miss) * 0.5)" not in _vsrc, \
        "PC 端还在按整轮固定时长判超时"
    assert "last_n, idle = len(got), time.time()" in _vsrc, \
        "PC 端没有「有新帧就重置空闲计时」"
    assert "60 秒没有新帧" in _vsrc, "PC 端 25 秒无进展就重启，慢机器会反复重来"

    # 心跳语义模拟：把 App 里 watchdog / awaitFrame 的判定式搬到 Python 上跑。
    # 双向验证：慢但持续推进必须不超时，真的零进展必须超时；旧版固定 deadline
    # 与固定 120 秒等帧在同一场景下必须被判失败。
    _STALL, _FIRST = 300_000, 180_000

    def _watchdog(arrivals, end_ms):
        """arrivals: 帧数增长的时刻（ms）；返回 (是否卡死, 已收帧数)。"""
        hits = [0] + [t for t in arrivals if t <= end_ms]
        last, n = hits[-1], len(hits) - 1
        grace = _STALL if n > 0 else _FIRST
        return end_ms - last > grace, n

    def _await_frame(frame_ms, beats):
        """第 frame_ms 时刻目标帧落盘；beats 是全局心跳（其它帧在推进）。"""
        last = 0
        for t in sorted(set([0] + [b for b in beats if b < frame_ms])):
            if t - last > _STALL:
                return False
            last = t
        return frame_ms - last <= _STALL

    # 每 4 分钟才出一帧、整整 40 分钟：旧版 12 分钟就砍，新版必须跑完
    _slow = [240_000 * k for k in range(1, 11)]
    assert _watchdog(_slow, 40 * 60_000) == (False, 10), \
        "慢但持续推进被误判为超时"
    assert 40 * 60_000 >= 12 * 60_000, "对照场景必须先超过旧的 12 分钟上限"
    assert _watchdog([], _FIRST + 1)[0], "首帧一直不来必须判卡死"
    assert _watchdog([60_000], 60_000 + _STALL + 1)[0], \
        "收到首帧后连续零进展必须判卡死"
    assert not _watchdog([60_000], 60_000 + _STALL)[0], \
        "刚好到宽限边界不该提前判死"
    # 一帧等 10 分钟，但期间渲染侧每 60 秒都在出别的帧：旧版 120 秒就返回 null
    assert _await_frame(600_000, [60_000 * k for k in range(1, 10)]), \
        "全局还在推进时，等单帧不该被 120 秒死线掐断"
    assert 600_000 > 120_000, "对照场景必须先超过旧的 120 秒等帧上限"
    assert not _await_frame(600_000, [60_000, 120_000]), \
        "心跳停了超过宽限还一直空等，卡死就发现不了"
    # 超时重试会换一个 run 重头渲，旧帧目录（几百 MB）不清理会一直堆着。
    # 只允许删「过期的合法 run 目录」，正在收帧的新 run 绝不能误删。
    assert "\n        _sweep_motion_frames()\n" in _ap_src, \
        "重试会不断留下新 run 的帧目录，没有清理入口（注释掉不算）"
    with _tf.TemporaryDirectory() as _sd:
        _sroot = _Path(_sd)
        _fr = _sroot / "_frames"
        _fr.mkdir()
        _old, _new, _weird = (_fr / "aabbcc112233", _fr / "ddeeff445566",
                              _fr / "not-a-run!")
        for _d in (_old, _new, _weird):
            _d.mkdir()
            (_d / "f_00000.jpg").write_bytes(b"x")
        (_fr / "keep.txt").write_text("x", encoding="utf-8")
        import time as _time
        _stale = _time.time() - 25 * 3600
        _os.utime(_old, (_stale, _stale))
        with _mock.patch.object(AP, "OUT", _sroot):
            assert AP._sweep_motion_frames() == 1, \
                "过期帧目录没被清掉（或误删了别的目录）"
        assert not _old.exists() and _new.exists() and _weird.exists(), \
            "清理误删了正在用的新 run 或非法命名目录"
        assert (_fr / "keep.txt").is_file(), "清理把普通文件也删了"
    # ---- v45：底图视口自适应（放大裁切）+ 视频卡片黑屏频闪 ------------------
    # 高德 zoom 只能是整数，`_fit_zoom` 选「刚好装得下」的那一档，于是某些
    # 活动的轨迹会撑到画幅 90%（看着像底图被放大裁切），另一些只占 30%。
    # 修法是底图位图与所有轨迹坐标共用同一组 (k,dx,dy) 仿射变换。
    assert "def _view_fit" in _msrc and '"view": view' in _msrc, \
        "底图与轨迹没有共用同一组视口变换"
    assert "g.drawImage(im, 0, 0, nw, nh, V.dx, V.dy, W * V.k, H * V.k)" in _msrc, \
        "bakeBase 没按 view 绘制底图，轨迹和路网会错开"
    assert "g.fillStyle = '#0E131A'; g.fillRect(0, 0, W, H);" in _msrc, \
        "视口缩小时画布边缘会露出纯黑"
    assert 'pts = [(x * view["k"] + view["dx"], y * view["k"] + view["dy"])' in _msrc, \
        "轨迹点没跟着底图做同一变换"
    assert '"view": pl.get("view") or {}' in _ap_src, \
        "App 拿不到取景参数，真机没法定位底图问题"
    assert "view: D.view || null" in _msrc and "baseSize:" in _msrc, \
        "诊断没回传视口参数与底图实际解码尺寸"
    # 三种占比都要落到目标区间；变换必须是同一个仿射（距离比 = k）
    for _bw, _bh, _label in ((0.84, 0.91, "放大裁切"),
                             (0.30, 0.36, "缩在一角"),
                             (0.59, 0.73, "正常")):
        _pad = [(540 - 1080 * _bw / 2 + 1080 * _bw * i / 8,
                 960 - 1920 * _bh / 2 + 1920 * _bh * i / 8)
                for i in range(9)]
        _v = MO._view_fit(_pad)
        _after = [(x * _v["k"] + _v["dx"], y * _v["k"] + _v["dy"])
                  for x, y in _pad]
        _rw = (max(p[0] for p in _after) - min(p[0] for p in _after)) / 1080
        _rh = (max(p[1] for p in _after) - min(p[1] for p in _after)) / 1920
        assert _rw <= MO.VIEW_W + 0.01 and _rh <= MO.VIEW_H + 0.01, \
            f"{_label}：变换后轨迹仍顶到边缘"
        assert _rw >= MO.VIEW_W * 0.55 and _rh >= MO.VIEW_H * 0.55, \
            f"{_label}：变换后轨迹还是缩在一角"
        assert abs(abs(_after[1][0] - _after[0][0]) -
                   abs(_pad[1][0] - _pad[0][0]) * _v["k"]) < 0.01, \
            "底图与轨迹用的不是同一组变换"
    # 视频：`seeked` 不代表新帧已解码，真机 WebView 会画出黑帧。
    # 只有亮度达标的帧才更新缓存，黑帧复用上一张有效画面。
    assert "const VLAST = new Map()" in _msrc and "function videoFrame(" in _msrc, \
        "视频卡片没有黑帧探测与上一帧缓存"
    assert "lit = sum > 24 * 24 * 3 * 8" in _msrc and "getImageData" in _msrc, \
        "黑帧判据丢了"
    assert "videoFrame(p.clip.src, v, w, h)" in _msrc, \
        "drawPop 还在直接画 <video>，黑帧会闪出来"
    assert "ctx.drawImage(v, left + pad" not in _msrc, \
        "还留着直连 <video> 的绘制路径"
    assert "VLAST.clear()" in _msrc and "lastClipSrc" in _msrc, \
        "切换素材窗口没清缓存，会闪上一段的末帧"

    # ---- v46：配速角标频闪 + 视频段逐帧 seek 拖垮导出 ----------------------
    # 回放的 T 是 _synth_route 按里程现场合成的：420 点覆盖 5312 秒、还有
    # 几十处重复时间戳。旧 paceAt 拿 `T[ib]-T[ia]` 当 15 秒窗口，窗口里
    # 经常 ia === ib → dt=0 → fmtPace(null) 画 '—'，下一帧又有值，于是
    # 720 帧里 180 帧是 '—'、交替 207 次，看起来就是配速一直在闪。
    assert "const PACE_DT = 2" in _msrc and "const PACE_WIN = 15" in _msrc, \
        "没有按轨迹秒重采样累计距离"
    assert "buildPaceTable" in _msrc and "Float64Array" in _msrc, \
        "配速距离表没建出来"
    assert "T[ib] - T[ia]" not in _msrc, \
        "还在用稀疏时间戳的索引差当窗口时间，配速必然闪 '—'"
    assert "dt = b - a, dd = paceDist(b) - paceDist(a)" in _msrc, \
        "窗口时间没用 rt 本身、距离没用插值表"
    assert "return lastPace" in _msrc and "lastPace + (p - lastPace) * 0.30" in _msrc, \
        "缺上一有效值兜底与平滑，静止/GPS 漂移时还是会跳空值"
    assert "D.dur / DIST[N - 1] * 1000" in _msrc, \
        "首帧没有用全段平均配速起步，会先闪一下 '—'"
    # 视频段：随机 seek 是整条流水线最贵的一步，而旧阈值 0.04 在 24fps
    # （帧间隔 0.0417s）下永远不命中，等于每帧都 seek 一次；另有
    # waitReady(4000) 每帧最多空等 4 秒、11 段视频各存一张全分辨率首帧。
    assert "const CLIP_FPS = 12" in _msrc and "const VSTEP = new Map()" in _msrc, \
        "视频卡片没有抽帧网格，导出仍会逐帧 seek"
    assert "const VDONE = new Map()" in _msrc and "const WAITED = new Set()" in _msrc, \
        "缺「同格只抓一次 / 只等一次就绪」的状态"
    assert "Math.floor(VREL * CLIP_FPS)" in _msrc, \
        "回退路径的 seek 目标没量化到抽帧网格（v47 起 rel 统一存进 VREL）"
    assert "if (VSTEP.get(src) === step) return;" in _msrc, \
        "同一抽帧格里还是会重复 seek"
    assert "VDONE.get(src) === step" in _msrc, \
        "同一抽帧格里还是会每帧做亮度探测 + 全尺寸重绘"
    assert "function fallbackFrame(" in _msrc and \
        "return fallbackFrame(src, w, h);   // 黑帧沿用上一张有效画面" in _msrc, \
        "黑帧（没有缓存）时同格仍会反复探测，视频解不出画面就又拖慢导出"
    assert "setTimeout(done, 350)" not in _msrc and "setTimeout(done, 120)" in _msrc, \
        "seek 超时仍是 350ms，几千帧会积成几十分钟"
    assert _msrc.count("v.removeEventListener('seeked', done);") >= 2, \
        "预抽 seekExact / 回退路径 有一处超时后没清理 seeked 监听"
    assert "waitReady(v, 4000)" not in _msrc and "first ? 1500 : 250" in _msrc, \
        "还在每帧等最多 4 秒视频就绪（v47 起每次只短等 250ms）"
    assert "Math.min(840 / iw, 780 / ih, 1)" in _msrc, \
        "首帧兜底没缩到卡片尺寸，11 段原分辨率 canvas 会吃满 WebView 内存"
    assert "const w = v.videoWidth || 1080, h = v.videoHeight || 1920" not in _msrc, \
        "首帧兜底还在按视频原始分辨率建常驻画布"
    assert "VSTEP.clear(); VDONE.clear();" in _msrc, \
        "切换素材窗口没重置抽帧格，会串到上一段"

    # ---- v47：视频画面中途冻结 + 渲染循环仍被随机 seek 拖住 ----------------
    # 冻结：Android WebView 在 seek 期间 readyState 会短暂掉到 1。旧版
    # WAITED 只等第一次、之后每格直接放弃；VSTEP 又在 seek **之前**置位，
    # 于是 120ms 超时那一格也被当成「已处理」，videoFrame 抓到 seek 未完成
    # 的旧帧（非黑，亮度判据过）缓存下来。连续超时时画面就停住不动，而音轨
    # 由原生 MediaExtractor 独立解码，所以「画面暂停、声音正常」。
    assert "const VPRE = {" in _msrc and "async function preloadClip(" in _msrc, \
        "没有导出期按段预抽帧，渲染循环还得逐格 seek <video>"
    assert "const PRE_MAX = 150" in _msrc and "PRE_SEEK_MS = 150" in _msrc, \
        "PC 兜底预抽没有格数/单格耗时上限"
    assert "const PRE_LONG = 480" in _msrc and "function preSize(" in _msrc, \
        "预抽画布没缩到卡片尺寸，几百格原分辨率会吃满 WebView"
    assert "await seekExact(v, Math.min(" in _msrc and "const lit = probeLit(v);" in _msrc, \
        "预抽没有严格等 seeked 与黑帧探测"
    assert "if (!WAITED.has(src))" not in _msrc, \
        "还在「等过一次就绪就永远不等」，readyState 掉到 1 时恢复不了"
    assert "first ? 1500 : 250" in _msrc, \
        "回退路径没有每次短等就绪"
    _vstep_set = _msrc.find("VSTEP.set(src, step)")
    _want_seek = _msrc.find("try { v.currentTime = want; }")
    assert 0 < _want_seek < _vstep_set, \
        "VSTEP 又提前到 seek 之前了，超时那一格会把旧帧当成新画面缓存"
    _pre_ret = _msrc.find("VSTEP.set(src, Math.floor(VREL * VPRE.fps));\n    return;")
    assert 0 < _pre_ret < _want_seek, \
        "预抽就绪时没在 seek 之前返回，渲染循环仍在随机 seek"
    assert _msrc.count("exportMode && VPRE.src === src && VPRE.frames.length") >= 2, \
        "syncVideo / videoFrame 没有都走预抽缓存分支"
    assert "exportMode = true" in _msrc and "await preloadClip(clip)" in _msrc, \
        "runExport 没有在进入素材窗口时预抽"
    assert "if (!src && VPRE.frames.length)" in _msrc, \
        "切出素材窗口没释放缓存，长片会堆满小画布"
    assert "if (!v) { VPFAIL.add(clip.src); return; }" in _msrc and \
        "if (v.readyState < 2) { VPFAIL.add(clip.src); return; }" in _msrc and \
        "!VPFAIL.has(src)" in _msrc, \
        "预抽失败没有回退路径，视频解不出画面会把导出卡死"
    assert "preload: info" in _msrc and "motion preload:" in _main, \
        "真机拿不到每段预抽结果，出问题没法定位"
    # 预抽索引映射：短素材保持 12fps；长素材自动降格率，索引仍单调不越界
    for _dur47, _label in ((6.0, "6 秒"), (60.0, "60 秒"), (256.0, "长片")):
        _n47 = max(1, min(150, int(_dur47 * 12 + 0.999)))
        _fps47 = _n47 / _dur47
        _seq47 = [max(0, min(_n47 - 1, int(_i / 24 * _fps47)))
                  for _i in range(int(_dur47 * 24))]
        assert len(_seq47) > 0 and _seq47[0] == 0, f"{_label}：首格不对"
        assert all(b >= a for a, b in zip(_seq47, _seq47[1:])), \
            f"{_label}：画面索引回退，成片里视频会倒放"
        assert max(_seq47) < _n47, f"{_label}：索引越界会取到空帧"
        assert _n47 <= 150, f"{_label}：单段预抽超过内存上限"
    # 预抽语义模拟：旧路径在渲染循环里每换一格就随机 seek 一次；新路径整段
    # 只在进入窗口时顺序抽一次，渲染循环零 seek。零 seek 既消除了逐帧解码
    # 等待，也让「seek 超时 → 抓到旧帧 → 画面停住」这条链不再存在。
    def _render47(_dur, _frames, preload):
        _n = max(1, min(150, int(_dur * 12 + 0.999)))
        _fps = _n / _dur
        _seeks, _idx = 0, []
        for _i in range(_frames):
            _rel = _i / 24
            if not preload:
                _prev = int(max(0, (_i - 1) / 24) * 12)
                if int(_rel * 12) != _prev:
                    _seeks += 1
            _idx.append(max(0, min(_n - 1, int(_rel * _fps))))
        return _seeks, _idx

    _old_seek, _ = _render47(6.0, 48, False)
    _new_seek, _new_idx = _render47(6.0, 48, True)
    assert _old_seek > 0 and _new_seek == 0, \
        "渲染循环里还在随机 seek，画面冻结与耗时问题都会回来"
    assert _new_idx[-1] > _new_idx[0] and \
        all(b >= a for a, b in zip(_new_idx, _new_idx[1:])), \
        "预抽画面索引没有随播放时间推进，成片视频会停住"

    # ---- v48：把抽帧从 WebView 挪到原生 -----------------------------------
    # 真机症状：「第 219 帧没送到（导出中断）」= 编码线程连续 5 分钟没等到新帧。
    # 219/24 ≈ 9.1 秒，正好落在第一段素材窗口开始处：页面在预抽那一段时被
    # `drawImage(<video>)` **同步**阻塞，主线程占住后连 setTimeout 兜底都跑不了。
    # v46/v47 只是把「逐格随机 seek」换成「整段顺序 seek」，总成本没降。
    _clips_kt = _kt_dir / "MotionClips.kt"
    assert _clips_kt.is_file(), \
        "没有原生抽帧实现，WebView 里抽一段 6 秒素材仍要 5 分钟"
    _ck = _clips_kt.read_text(encoding="utf-8")
    assert "object MotionClips {" in _ck, "原生抽帧入口被改名/删掉"
    assert _ck.count("getScaledFrameAtTime") >= 2, \
        "原生抽帧没用缩略帧 API（精确帧 + 关键帧两条都要）"
    assert "OPTION_CLOSEST" in _ck and "OPTION_CLOSEST_SYNC" in _ck, \
        "精确帧失败没有退回关键帧，个别编码器上会整段抽不到"
    assert "bmp.recycle()" in _ck and "FileOutputStream" in _ck, \
        "抽帧没有逐帧回收 Bitmap / 落盘 JPEG，几十张原图就 OOM"
    assert "METADATA_KEY_VIDEO_ROTATION" in _ck, \
        "竖拍视频没按 rotation 交换宽高，卡片里会横过来"
    assert "prepareClipFrames(r)" in _main, \
        "App 没有在起 WebView 前预抽素材帧"
    _pre_call = _main.find("prepareClipFrames(r)")
    _start_view = _main.find("startMotionView(\n")
    assert 0 < _pre_call < _start_view, \
        "抽帧跑到 startMotionView 之后了，会撞上「5 分钟没有新帧」判死"
    assert '"clips": clip_list' in _ap_src and \
        '"dir": str(OUT / "_frames" / run)' in _ap_src, \
        "motion_html 没把素材清单 / 帧目录交给原生"
    assert '"ci": (p["clip"].get("ci")' in _ap_src and \
        '"file": p["clip"].get("local")' in _ap_src, \
        "素材清单缺下标或本地路径，原生抽不了"
    assert 'item["ci"] = out_clips' in _msrc, "payload 里的 clip 没有 ci 下标"
    assert "const CIMG = new Map()" in _msrc and \
        "async function loadClipImages(" in _msrc, \
        "页面没有原生帧加载入口，还在 WebView 里抽"
    assert "await loadClipImages(run);" in _msrc, "runExport 没先加载原生帧"
    _load_call = _msrc.find("await loadClipImages(run);")
    _loop48 = _msrc.find("for (let i = from; i < total; i++)")
    assert 0 < _load_call < _loop48, \
        "原生帧在逐帧循环之后才加载，等于没用"
    assert "CIMG.has(ci)" in _msrc, "preloadClip 没有优先用原生帧"
    assert _msrc.count("if (exportMode && VPFAIL.has(src))") >= 2, \
        "抽帧失败没有在 syncVideo / videoFrame 两处都拦成静态封面"
    assert "if (exportMode && CIREADY) { VPFAIL.add(clip.src); return; }" in _msrc, \
        "清单读到了却没有这段素材时，还是会回 WebView 里抽"
    _ci_ret = _msrc.find(
        "if (exportMode && CIREADY) { VPFAIL.add(clip.src); return; }")
    _fallback48 = _msrc.find("const v = VIDS.get(clip.src);")
    assert 0 < _ci_ret < _fallback48, \
        "CIREADY 的静态封面分支在 WebView 抽帧之后，App 端仍会走同步解码那条路"
    assert "im.decode ? im.decode().catch" in _msrc, \
        "原生帧没有预解码，首帧 drawImage 还会卡在解码上"
    # 原生帧索引：n 被原生 maxFrames=150 截断，fps=n/dur，页面按 floor(rel*fps)
    for _dur48, _n48 in ((6.0, 72), (30.0, 150), (256.0, 150)):
        _f48 = _n48 / _dur48
        _seq48 = [max(0, min(_n48 - 1, int(_i / 24 * _f48)))
                  for _i in range(int(_dur48 * 24))]
        assert _seq48[0] == 0 and _seq48[-1] < _n48, \
            f"{_dur48}s 素材的原生帧索引越界"
        assert all(b >= a for a, b in zip(_seq48, _seq48[1:])), \
            f"{_dur48}s 素材的原生帧索引会倒退，成片里视频倒放"

    # 数值模拟（不依赖外部 DB）：稀疏 + 重复时间戳的路线，新旧算法逐帧对比。
    _pt = [0, 10, 25, 25, 40, 66, 80, 100, 100, 130, 150,
           180, 200, 200, 230, 260, 300]
    _pd = [0, 15, 35, 35, 60, 95, 120, 155, 155, 200, 230,
           275, 305, 305, 350, 395, 455]
    _dur, _play, _n = 300, 30, len(_pt)

    def _idx(t):
        _lo, _hi = 0, _n - 1
        while _lo < _hi:
            _m = (_lo + _hi) >> 1
            if _pt[_m] < t:
                _lo = _m + 1
            else:
                _hi = _m
        return _lo

    def _pace_old(rt):
        _a, _ia, _ib = max(rt - 15, 0), _idx(max(rt - 15, 0)), _idx(rt)
        _dd, _dt = _pd[_ib] - _pd[_ia], _pt[_ib] - _pt[_ia]
        return (_dt / _dd * 1000) if (_dd > 5 and _dt > 0) else None

    _PDN = max(2, -(-_dur // 2) + 1)
    _table, _k = [], 0
    for _i in range(_PDN):
        _t = _i * 2
        while _k + 1 < _n and _pt[_k + 1] <= _t:
            _k += 1
        if _k + 1 >= _n:
            _v = _pd[_n - 1]
        elif _pt[_k + 1] <= _pt[_k]:
            _v = _pd[_k + 1]
        else:
            _f = min(1, max(0, (_t - _pt[_k]) / (_pt[_k + 1] - _pt[_k])))
            _v = _pd[_k] + (_pd[_k + 1] - _pd[_k]) * _f
        _table.append(_v)

    def _dist(t):
        _x = min(max(t / 2, 0), _PDN - 1)
        _i, _f = int(_x), _x - int(_x)
        _j = min(int(_x) + 1, _PDN - 1)
        return _table[_i] + (_table[_j] - _table[_i]) * _f

    _last = _dur / _pd[-1] * 1000

    def _pace_new(rt):
        nonlocal _last
        _b, _a = min(max(rt, 0), _dur), max(min(max(rt, 0), _dur) - 15, 0)
        _dt, _dd = _b - _a, _dist(_b) - _dist(_a)
        if _dt > 0 and _dd > 1:
            _p = _dt / _dd * 1000
            if 60 < _p < 2400:
                _last = _last + (_p - _last) * 0.30
        return _last

    _seq_old = [_pace_old(min(i / 24 * (_dur / _play), _dur))
                for i in range(_play * 24)]
    _seq_new = [_pace_new(min(i / 24 * (_dur / _play), _dur))
                for i in range(_play * 24)]
    _flips_old = sum(1 for a, b in zip(_seq_old, _seq_old[1:])
                     if (a is None) != (b is None))
    assert sum(1 for v in _seq_old if v is None) > 0 and _flips_old > 0, \
        "模拟样本没复现旧算法的 '—' 交替，断言失去意义"
    assert all(v is not None and 60 < v < 2400 for v in _seq_new), \
        "新算法仍会出现空值或离谱配速"
    assert all(abs(b - a) < 60 for a, b in zip(_seq_new, _seq_new[1:])), \
        "新配速逐帧跳变过大，还是会看出闪"
    # 抽帧网格：24fps 下连续 24 帧只应落到 12 个不同的 seek 目标
    _wants_old = [i / 24 for i in range(24)]
    _wants_new = [_w // (24 // 12) / 12 for _w in range(24)]
    assert len(set(_wants_old)) == 24 and len(set(_wants_new)) == 12, \
        "抽帧网格没把 seek 次数减半"
    # 黑帧语义模拟：亮帧更新、黑帧复用、切窗口清空
    def _vframes(seq, switch_at=None):
        cache, out = None, []
        for i, lum in enumerate(seq):
            if switch_at is not None and i == switch_at:
                cache = None
            if lum > 8:
                cache = ("frame", i)
            out.append(cache)
        return out

    _vf = [30, 0, 40, 0, 0]
    _vgot = _vframes(_vf)
    assert _vgot[1] == _vgot[0] and _vgot[3] == _vgot[2] == ("frame", 2), \
        "黑帧没有复用上一张有效画面"
    assert _vframes(_vf, switch_at=4)[4] is None, \
        "切素材窗口没清缓存，第一帧会闪上一段的末帧"
    # 百分比序列：min 语义、单调、两端对齐、绝不假跳
    def _pct(r, e, total):
        return min(r, e) * 100 // max(1, total)
    _seq = [_pct(n, n, 720) for n in range(0, 721, 4)]
    assert _seq[0] == 0 and _seq[-1] == 100 and _seq == sorted(_seq), \
        "百分比会回退或到不了 100"
    assert all(0 <= p <= 100 for p in _seq), "百分比越界"
    assert _pct(1, 1, 720) < 5, "第一帧就跳到 70%，进度条又虚高了"
    assert _pct(700, 100, 720) == _pct(100, 100, 720), \
        "渲染快、编码慢时没按较慢的一条显示"
    # 真机没法逐帧比对 YUV，先在 PC 上锁死「查表 + 2×2 展开」与原公式等价：
    # 任何符号写反、除法定点写错都会让整条视频偏色。
    import random as _random
    _rnd = _random.Random(7)
    for _ in range(400):
        _px = [(_rnd.randrange(256), _rnd.randrange(256), _rnd.randrange(256))
               for _ in range(4)]
        _old = [sum(c[k] for c in _px) // 4 for k in range(3)]
        _new = [(_px[0][k] + _px[1][k] + _px[2][k] + _px[3][k]) // 4
                for k in range(3)]
        assert _old == _new, "2×2 展开求和与原内层循环不等价"
        _r, _g, _b = _new
        assert ((-38 * _r - 74 * _g + 112 * _b + 128) >> 8) + 128 == \
            (((-38 * _r) + (-74 * _g) + (112 * _b) + 128) >> 8) + 128, \
            "U 查表与原公式不等价"
        for _v in (((66 * _r + 129 * _g + 25 * _b + 128) >> 8) + 16,
                   ((-38 * _r - 74 * _g + 112 * _b + 128) >> 8) + 128,
                   ((112 * _r - 94 * _g - 18 * _b + 128) >> 8) + 128):
            assert 0 <= _v <= 255, "YUV 分量溢出 Byte，toByte 会绕回造成色斑"
    # ---- 素材必须「完整插入」：一段都不许丢，同地两段也不许被合并掉 ----
    _frt = {"n": 3, "t0": 1_700_000_000_000, "t": [0, 60, 120],
            "lat": [30.0, 30.001, 30.002], "lon": [120.0, 120.001, 120.002],
            "d": [0, 100, 200], "hr": [120, 130, 140], "seg": [0],
            "v": [1.6, 1.7, 1.8]}
    with _tf.TemporaryDirectory() as _ptd:
        _pp = _Path(_ptd)

        def _mkclip(name, kind, dur, t_utc, lat=None, lon=None):
            p = _pp / name
            p.write_bytes(b"x")
            return {"path": str(p), "name": name[:-4], "kind": kind, "dur": dur,
                    "w": 1080, "h": 1440 if kind == "image" else 1920,
                    "t_utc": t_utc, "lat": lat, "lon": lon}

        _fclips = [
            # 同一地点两段视频（只差 1 秒）：合并掉一段就等于「素材丢了」
            _mkclip("a.mp4", "video", 8.0, 1_700_000_030_000, 30.0005, 120.0005),
            _mkclip("b.mp4", "video", 5.0, 1_700_000_031_000, 30.0005, 120.0005),
            _mkclip("c.jpg", "image", 3.0, 1_700_000_070_000, 30.0012, 120.0012),
            # 时间差 3 小时（明显不在这次活动里）：贴到最近的一端，不许丢
            _mkclip("d.mp4", "video", 4.0, 1_700_000_000_000 - 3 * 3600_000),
        ]
        with _mock.patch.object(MOT, "ensure_basemap", lambda *a, **k: None), \
                _mock.patch.object(MOT.clip_mod, "ensure_playable",
                                   lambda i, c: i["path"]), \
                _mock.patch.object(MOT.clip_mod, "probe",
                                   lambda p: {"w": 1080, "h": 1920}):
            _fpl = MOT.build_payload({"name": "测", "start_ts": "2026-11-07 07:00:00"},
                                     {"route": _frt}, None, clips=_fclips,
                                     duration=30.0)
        _got_names = sorted(p["clip"]["name"] for p in _fpl["photos"] if p.get("clip"))
        assert _got_names == ["a", "b", "c", "d"], f"素材没完整插入：{_got_names}"
        # 混音要的磁盘路径必须一直在，且等于页面看到的源（web=True 时只改 src）
        assert all(_Path(p["clip"]["local"]).is_absolute()
                   and p["clip"]["local"] == p["clip"]["src"]
                   for p in _fpl["photos"] if p.get("clip")), \
            "素材没带可解码的本地路径"
        assert not _fpl["loose"], f"素材被判成挂不上了：{_fpl['loose']}"
        # 30 + 8 + 5 + 3 + 4，一张不少
        assert _fpl["total"] == 50.0, _fpl["total"]
        _clamp = next(p for p in _fpl["photos"] if (p.get("clip") or {}).get("name") == "d")
        assert _clamp["t"] == 0.0 and _clamp["clip"]["approx"], "越界素材没贴到起点"
    # 素材清单要够界面用：类型、文件是否还在、拍摄时间（列表要显示缩略图和时间）
    assert 'it["exists"] = p.is_file()' in _ap_src, "素材清单没标文件是否存在"
    assert 'it["t_utc"] = _meta_val(it, info, "t_utc")' in _ap_src, "素材清单没带拍摄时间（且应优先用存下来的）"
    assert '"t_source"' in _ap_src, "素材清单没带时间来源"
    # 安卓侧：必须把相册原始文件名带进登记（相机名里常带拍摄时间），
    # 并且列表要说明「没有拍摄时间」的后果，不能只显示一行「加入 xx」。
    # 手机上没有 android/ 目录，这里只在桌面自检时校验。
    _kt = Path(AP.__file__).parent.parent / "android/app/src/main/java/com/garmincoach/app/MainActivity.kt"
    if _kt.is_file():
        _kt_src = _kt.read_text(encoding="utf-8")
        assert "OpenableColumns.DISPLAY_NAME" in _kt_src, "安卓没读相册原始文件名"
        assert "dst.absolutePath, displayName" in _kt_src, "登记时没把原始文件名传给后端"
        assert "无拍摄时间" in _kt_src, "素材列表没说明缺少拍摄时间"
        assert "重新读取时间" in _kt_src, "老素材没有补读入口"
        # 真机上「相册显示有拍摄时间、App 却说没有」：时间只在 MediaStore /
        # MediaMetadataRetriever 的日期标签里，裁完的 mp4 根本不带。必须在选片
        # 那一刻取走，并作为最后一档兜底传进 clips_add。
        assert "METADATA_KEY_DATE" in _kt_src, "没从视频解码器读拍摄日期"
        assert '"datetaken"' in _kt_src, "没从相册读 DATE_TAKEN"
        assert "src.absolutePath, takenMs" in _kt_src, "裁切前时间没传给 clips_add"
        assert "clips_reprobe" in _kt_src, "「重新读取时间」没真的调后端重探"
        assert "parseMediaDate" in _kt_src, "视频日期标签没解析"
    assert '"clips_reprobe"' in _ap_src, "clips_reprobe 没注册进 App 接口白名单"
    assert "taken_ms" in _ap_src and "t_native" in _ap_src, "原生兜底时间没落盘"
    assert "def clips_reprobe" in _ap_src, "缺重探接口"
    _ph = next(iter((HOME / "data" / "photos").rglob("*.jpg")), None) if (
        HOME / "data" / "photos").is_dir() else None
    if _ph is not None:
        _mi = AP._probe_material(_ph)
        assert _mi["kind"] == "image", _mi
        assert _mi.get("t_utc"), f"图片素材没读出拍摄时间：{_ph}"
    # 登记素材必须从**源文件**取拍摄时间：MediaMuxer 重封装会丢掉 mvhd.creation_time，
    # 只探成品就永远没有时间，回放里只能按数量均匀铺开、位置不准。
    from garmin_cn import clip as _CL
    _mcd = HOME / "data" / "motion_cache"
    _mp4s = sorted(_mcd.glob("*.mp4")) if _mcd.is_dir() else []
    # 只认「文件里真的写了 mvhd.creation_time」的样本。`name_hint="x.mp4"` 是
    # 故意的：屏蔽「从文件名兜时间」，免得把靠名字补出时间的副本误当源片。
    _has_t = next((p for p in _mp4s
                   if _CL.probe(p, name_hint="x.mp4").get("t_source") == "mvhd"), None)

    # 拍摄时间的多来源兜底：相机文件名 / udta 文本 / 写回成品 mvhd。
    # 这条链路决定「概览能不能显示拍摄时间」和「老素材能不能自己补回来」。
    _t = _CL.time_from_name("VID_20261103_180036.mp4")
    assert _t and dt.datetime.fromtimestamp(
        _t / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-11-03 10:00", \
        "相机文件名里的时间戳没解析对"
    assert _CL.time_from_name("clip-1730966400000.mp4") is None, "无日期的文件名不该凭空造时间"
    assert _CL.time_from_name("PXL_20261103_180036123.mp4"), "Pixel 文件名尾部的毫秒不该挡住解析"
    assert "stamp_creation(p, int(t_utc))" in _ap_src, "登记时没把拍摄时间写回成品"
    assert "name_hint=name_hint" in _ap_src, "登记/清单没把原始文件名传给视频探测"
    assert "name_hint" in Path(_CL.__file__).read_text(encoding="utf-8"), "视频探测没接原始文件名兜底"
    assert 'it["t_why"]' in _ap_src, "素材清单没给「读不到拍摄时间」的原因"
    assert "repairs" in _ap_src, "老素材没有补读落盘"

    # 「没有拍摄时间」的成品**自己造**：把真实样本的 mvhd 时间清零。
    # 不依赖本机缓存里恰好有转码副本，也不依赖被测的解析逻辑来挑样本
    # （否则改坏了样本就消失，整块断言会被静默跳过）。
    if _has_t is not None:
        _old_clips = AP.CLIPS
        try:
            with _tf.TemporaryDirectory() as _td:
                AP.CLIPS = Path(_td) / "clips.json"
                _src_t = _CL.probe(_has_t, name_hint="x.mp4")["t_utc"]

                def _fresh(n: str) -> Path:
                    d = Path(_td) / n
                    d.write_bytes(_has_t.read_bytes())
                    buf, moov, base = _CL._moov_view(d)
                    mv = _CL._walk(buf, moov[0], moov[1], "mvhd")
                    sz = 8 if buf[mv[0]] == 1 else 4
                    with d.open("r+b") as f:
                        f.seek(base + mv[0] + 4)
                        f.write(b"\0" * (2 * sz))   # creation + modification 清零
                    return d

                # 1) 源文件继承：两条各用一份成品（clips_add 会原地写回时间，
                #    共用一个文件会把第一条也「补」上，断言就假通过了）
                _d_old, _d_new = _fresh("old.mp4"), _fresh("new.mp4")
                AP.clips_add(str(_d_old), "旧式", 3.0, 1080, 1920, "video")
                AP.clips_add(str(_d_new), "新式", 3.0, 1080, 1920, "video", str(_has_t))
                _its = {x["name"]: x for x in AP.clips_list()["items"]}
                assert not _its["旧式"].get("t_utc"), "样本选错了：成品本身就有时间"
                assert _its["新式"].get("t_utc"), "登记时没从源文件继承拍摄时间"
                assert _its["新式"].get("t_source") == "mvhd", _its["新式"].get("t_source")
                assert _CL.probe(_d_new, name_hint="x.mp4").get("t_utc"), \
                    "成品没被写回拍摄时间，记录一丢就又读不回来了"

                # 2) 独立验证打标本身
                _dst = _fresh("clip-xyz.mp4")
                assert not _CL.probe(_dst).get("t_utc"), "打标前成品不该有时间"
                assert _CL.stamp_creation(_dst, _src_t), "打标失败"
                assert _CL.probe(_dst).get("t_utc") == _src_t, "打标后成品没读回拍摄时间"

                # 3) 老记录：没存过时间，但名字带摄像头时间戳 → 补读并落盘。
                #    必须用**没打标过**的另一份成品，否则会从 mvhd 读到时间，
                #    断言就抓不到「name_hint 没传」这种回归。
                _d2 = _fresh("clip-abc.mp4")
                AP.CLIPS.write_text(json.dumps({"items": [{
                    "id": "old1", "path": str(_d2), "name": "MVIMG_20261103_180036",
                    "kind": "video", "dur": 3.0, "t_utc": None}]}), encoding="utf-8")
                _it = AP.clips_list()["items"][0]
                assert _it.get("t_utc") == _CL.time_from_name("MVIMG_20261103_180036"), \
                    "老素材没从原始文件名补回拍摄时间"
                assert _it.get("t_source") == "name(+08)", _it.get("t_source")
                _raw = json.loads(AP.CLIPS.read_text("utf-8"))["items"][0]
                assert _raw.get("t_utc") == _it["t_utc"], "补读结果没写回 clips.json"
                assert "exists" not in _raw and "t_why" not in _raw, "临时字段被写进了记录"

                # 4) 补不回来的：必须给原因，界面不能只显示「加入 xx」让人猜
                _d3 = _fresh("clip-def.mp4")
                AP.CLIPS.write_text(json.dumps({"items": [{
                    "id": "old2", "path": str(_d3), "name": "clip-def",
                    "kind": "video", "dur": 3.0, "t_utc": None}]}), encoding="utf-8")
                _bad = AP.clips_list()["items"][0]
                assert not _bad.get("t_utc") and _bad.get("t_why"), \
                    "补不回时间时必须给出原因"

                # 5) 真机兜底：mp4 里完全没有时间，只有相册/解码器给的时间。
                #    用户看到的「有拍摄时间」往往就是这一份，裁完就没了。
                AP.CLIPS = Path(_td) / "native.json"
                _d4 = _fresh("clip-native.mp4")
                _r4 = AP.clips_add(str(_d4), "clip-native", 3.0, 1080, 1920,
                                  "video", "", _src_t)
                assert _r4["ok"] and _r4["t_utc"] == _src_t, "原生兜底时间没被采用"
                assert _r4["t_source"] == "相册/视频日期标签", _r4["t_source"]
                _it4 = AP.clips_list()["items"][0]
                assert _it4["t_utc"] == _src_t and _it4["t_native"] == _src_t, \
                    "原生兜底时间没存进记录"
                assert _CL.probe(_d4, name_hint="x.mp4").get("t_utc") == _src_t, \
                    "兜底时间没写回成品"
                assert AP._norm_taken_ms(_src_t / 1000) == _src_t, "秒级时间没换算"
                assert AP._norm_taken_ms(0) == 0 and AP._norm_taken_ms(1) == 0, \
                    "非法时间没挡住（会把加入时间当拍摄时间）"

                # 6) 「重新读取时间」：记录里存了兜底时间，重探要真能补回来
                _d5 = _fresh("clip-reprobe.mp4")
                AP.CLIPS.write_text(json.dumps({"items": [{
                    "id": "n1", "path": str(_d5), "name": "clip-reprobe",
                    "kind": "video", "dur": 3.0, "t_utc": None,
                    "t_native": _src_t}]}), encoding="utf-8")
                _rr = AP.clips_reprobe("n1")
                assert _rr["ok"] and _rr["t_utc"] == _src_t, _rr
                assert _CL.probe(_d5, name_hint="x.mp4").get("t_utc") == _src_t, \
                    "重探没把时间写回成品"
                assert not AP.clips_reprobe("missing")["ok"]
        finally:
            AP.CLIPS = _old_clips

    # 7) moov 落在文件中段的视频：head 1 MB / tail 8 MB 都够不着，必须靠顶层
    #    box 扫描。这也是真机「原片有拍摄时间、导入后却读不到」的一类根因。
    with _tf.TemporaryDirectory() as _td2:
        def _box(typ, payload):
            return (8 + len(payload)).to_bytes(4, "big") + typ.encode("ascii") + payload

        _secs = int((dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone.utc)
                     - _CL.EPOCH_1904).total_seconds())
        _mvhd = (b"\0" * 4 + _secs.to_bytes(4, "big") + _secs.to_bytes(4, "big")
                 + (1000).to_bytes(4, "big") + (3000).to_bytes(4, "big")
                 + b"\0" * 80)
        _mid = Path(_td2) / "mid-moov.mp4"
        _mid.write_bytes(_box("ftyp", b"isom" + b"\0" * 4 + b"isom")
                         + _box("mdat", b"\0" * (9 * 1024 * 1024))
                         + _box("moov", _box("mvhd", _mvhd)))
        _want = int((_CL.EPOCH_1904 + dt.timedelta(seconds=_secs)).timestamp() * 1000)
        _info = _CL.probe(_mid, name_hint="x.mp4")
        assert _info.get("t_utc") == _want and _info.get("t_source") == "mvhd", \
            f"中段 moov 没读到拍摄时间：{_info.get('t_why')}"
        assert _CL.stamp_creation(_mid, _want + 60000), "中段 moov 打标失败"
        assert _CL.probe(_mid, name_hint="x.mp4").get("t_utc") == _want + 60000, \
            "中段 moov 打标后没读回新时间"

    # 8) 底图缓存必须跟着取景走：同一活动换隐私档位（轨迹被裁短）后，
    #    绝不能复用旧底图，否则轨迹缩在画面一角，看着就是「底图缩放错」。
    from garmin_cn import motion as _MOT2, basemap as _BM2
    with _tf.TemporaryDirectory() as _td3:
        _cache = Path(_td3) / "basemaps"
        _cache.mkdir()
        _fake_png = b"\x89PNG\r\n\x1a\n" + b"0" * 2048
        _t_full = [[32.690, 109.000], [32.700, 109.010], [32.695, 109.005]]
        _t_trim = [[32.695, 109.005], [32.696, 109.006]]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return _fake_png

        with _mock.patch.object(_BM2, "CACHE", _cache), \
             _mock.patch.object(_BM2, "key", lambda: "fake"), \
             _mock.patch.object(_BM2, "regeo", lambda *a, **k: ""), \
             _mock.patch("urllib.request.urlopen", lambda *a, **k: _Resp()):
            _a = _MOT2.ensure_basemap(_t_full, "act1")
            _b = _MOT2.ensure_basemap(_t_trim, "act1")
            assert _a and _b, "假底图没取到"
            assert Path(_a["png"]) != Path(_b["png"]), \
                "不同取景复用了同一张底图（串档）"
            assert _a["meta"]["center"] != _b["meta"]["center"] or \
                _a["meta"]["zoom"] != _b["meta"]["zoom"], "两档取景参数不该相同"
            _a2 = _MOT2.ensure_basemap(_t_full, "act1")
            assert Path(_a2["png"]) == Path(_a["png"]), "同档位没命中缓存"
            # 旧版缓存（文件名不带指纹、rev=1）必须被彻底忽略
            _legacy = _cache / "motion-act1.png"
            _legacy.write_bytes(_fake_png)
            _legacy.with_name("motion-act1.json").write_text(json.dumps({
                "fit": "motion", "rev": 1, "frame": [1080, 1920],
                "center": [0.0, 0.0], "zoom": 4}), encoding="utf-8")
            _c = _MOT2.ensure_basemap(_t_full, "act1")
            assert Path(_c["png"]).name != _legacy.name, "旧缓存没失效"

    class _FakeClient:
        def __init__(self, items):
            self.items = items

        def activities(self, limit=60, start=0):
            return self.items[start:start + limit]

    with _tf.TemporaryDirectory() as td:
        items = [
            {"activityId": 1000 + i,
             "startTimeLocal": (dt.date(2026, 11, 6) -
                                dt.timedelta(days=i)).isoformat() + " 07:00:00",
             "activityType": {"typeKey": "running"},
             "duration": 1800, "distance": 6000}
            for i in range(150)
        ]
        # 补一年历史时活动不能只留最近 60 条：翻页应把 150 条都落库
        sa = _Store(_Path(td) / "a.db")
        sb = _Store(_Path(td) / "b.db")
        try:
            assert _Sync(_FakeClient(items), sa) \
                .sync_activities(max_pages=3, oldest=None) == 150
            # 翻到本页已有早于区间起点的活动时收手，不把整个账号历史拉一遍
            assert _Sync(_FakeClient(items), sb) \
                .sync_activities(max_pages=3, oldest="2026-11-01") == 60
        finally:
            # Windows 下不关连接，TemporaryDirectory 清理时会 WinError 32
            sa.conn.close()
            sb.conn.close()

    # 分享前轨迹脱敏：档位口径 + 「脱敏版绝不泄漏轨迹」的硬约束。
    # 用合成轨迹（约 1.1 km 直线），不碰任何真实活动 ID / 坐标 / 日期。
    from garmin_cn import privacy as PV
    assert PV.MODES == ("none", "trim", "hide")
    synth = [[30.0 + i * 0.0001, 120.0] for i in range(100)]
    _, ti = PV.mask_track(synth, "trim")
    assert 0 < ti["kept"] < 100 and ti["cut_head_m"] >= 200 and ti["cut_tail_m"] >= 200
    assert PV.mask_track(synth, "none")[1]["kept"] == 100
    assert PV.mask_track(synth, "hide")[0] == []
    assert PV.normalize("bogus") == "trim" and PV.normalize(None) == "none"
    priv_row = {"name": "自检活动", "date": "2026-11-05",
                "start_ts": "2026-11-05T17:46:00", "distance_m": 6200,
                "duration_s": 2100, "type": "running", "raw": "{}"}
    priv_detail = {"track": synth, "summary": {}, "laps": [],
                   "series": {"points": []}}
    h_none = ACTCARD.build_html(priv_row, priv_detail, privacy="none")
    h_trim = ACTCARD.build_html(priv_row, priv_detail, privacy="trim")
    h_hide = ACTCARD.build_html(priv_row, priv_detail, privacy="hide")
    assert '<path d="M' in h_none and 'data-gc-privacy="none"' in h_none
    assert "已脱敏" in h_trim and 'data-gc-privacy="trim"' in h_trim
    assert '<path d="M' not in h_hide and 'data-gc-private="hide"' in h_hide
    assert "轨迹已隐藏" in h_hide and "时间已隐藏" in h_hide
    assert "17:46" not in h_hide and "30.000" not in h_hide
    # 旧调用方不传档位时必须与 none 逐字节一致（兼容老 APK / 老 CLI）
    assert ACTCARD.build_html(priv_row, priv_detail) == h_none

    # 营养目标：三大营养素的 kcal 之和必须回到目标摄入（差几 kcal 是四舍五入），
    # 且目标摄入不能低于 BMR——这条被改坏等于鼓励长期吃不够。
    from garmin_cn import nutrition as NU

    nt = res.get("nutrition") or {}
    if nt.get("ok"):
        total = nt["protein_kcal"] + nt["carb_kcal"] + nt["fat_kcal"]
        assert abs(total - nt["target_kcal"]) <= 3, (total, nt["target_kcal"])
        assert nt["protein_g"] == round(nt["weight"] * 1.8), nt["protein_g"]
        assert not nt["bmr"] or nt["target_kcal"] >= nt["bmr"], nt
        assert sum(NU.pct_split(nt)) == 100, NU.pct_split(nt)
    out = render(res, store, tmp_html)
    assert out.exists() and out.stat().st_size > 5000

    # 轨迹合成：几何 + 时序合体后，时间/坐标/心率必须落在同一个点上
    from garmin_cn import photo as PH, route as RT

    lat0, lon0 = 34.1400, 108.8340
    poly = [{"lat": lat0 + i * 0.0001, "lon": lon0, "time": 1793659105000 + i * 1000,
             "valid": True, "speed": 0.0} for i in range(11)]
    det = {
        "geoPolylineDTO": {"polyline": poly},
        "metricDescriptors": [
            {"key": "directTimestamp", "metricsIndex": 0},
            {"key": "directHeartRate", "metricsIndex": 1},
            {"key": "directSpeed", "metricsIndex": 2},
            {"key": "sumDistance", "metricsIndex": 3},
        ],
        "activityDetailMetrics": [
            {"metrics": [1793659105000 + i * 1000, 120 + i, 2.5, i * 11.1]}
            for i in range(11)
        ],
    }
    rt = RT.build(det)
    assert rt["n"] == 11 and rt["t"][-1] == 10, rt
    assert rt["hr"][0] == 120 and rt["hr"][-1] == 130, rt["hr"]
    # 11 个点 × 0.0001° 纬差 ≈ 122 m（1° 纬度 110540 m）
    assert 110 < rt["d"][-1] < 135, rt["d"][-1]
    assert RT.summary(rt)["distance_m"] == rt["d"][-1]
    assert abs(RT.at(rt, 5)["d"] - rt["d"][5]) < 1.0
    assert RT.pace(rt, 10) is not None

    # EXIF：自己拼一张带 GPS 的 JPEG，验证解析（含 GPS UTC 时间与方向）
    import struct

    def _rat(v):
        d = int(v); m = int((v - d) * 60); s = round(((v - d) * 60 - m) * 60, 4)
        return struct.pack("<IIIIII", d, 1, m, 1, int(s * 10000), 10000)

    def _asc(x):
        return x.encode() + b"\x00"

    n0, nex, ng = 5, 3, 8
    o0 = 8
    oe = o0 + (2 + n0 * 12 + 4)
    og = oe + (2 + nex * 12 + 4)
    od = og + (2 + ng * 12 + 4)
    pool = bytearray()

    def _add(v):
        off = od + len(pool)
        pool.extend(v)
        if len(v) % 2:
            pool.append(0)
        return off

    # GPS 时间取轨迹首点的 UTC：对不上就落进「不在活动时段」，自检反而失效
    t0u = dt.datetime.fromtimestamp(1793659105000 / 1000, dt.timezone.utc)
    mk, md = _add(_asc("Xiaomi")), _add(_asc("Xiaomi 17"))
    dto = _add(_asc(t0u.strftime("%Y:%m:%d %H:%M:%S")))
    la, lo = _add(_rat(lat0)), _add(_rat(lon0))
    tm = _add(struct.pack("<IIIIII", t0u.hour, 1, t0u.minute, 1, t0u.second * 100, 100))
    gd = _add(_asc(t0u.strftime("%Y:%m:%d")))

    def _ent(t, ty, c, v):
        return struct.pack("<HHI", t, ty, c) + v

    i0 = (struct.pack("<H", n0)
          + _ent(0x010F, 2, 7, struct.pack("<I", mk)) + _ent(0x0110, 2, 10, struct.pack("<I", md))
          + _ent(0x0112, 3, 1, struct.pack("<HH", 6, 0))
          + _ent(0x8769, 4, 1, struct.pack("<I", oe)) + _ent(0x8825, 4, 1, struct.pack("<I", og))
          + struct.pack("<I", 0))
    ex = (struct.pack("<H", nex) + _ent(0x9003, 2, 20, struct.pack("<I", dto))
          + _ent(0xA002, 4, 1, struct.pack("<I", 4096))
          + _ent(0xA003, 4, 1, struct.pack("<I", 2304)) + struct.pack("<I", 0))
    gp = (struct.pack("<H", ng) + _ent(0x0001, 2, 2, b"N\x00\x00\x00")
          + _ent(0x0002, 5, 3, struct.pack("<I", la))
          + _ent(0x0003, 2, 2, b"E\x00\x00\x00")
          + _ent(0x0004, 5, 3, struct.pack("<I", lo))
          + _ent(0x0005, 1, 1, b"\x00\x00\x00\x00")
          + _ent(0x0006, 5, 1, struct.pack("<I", _add(struct.pack("<II", 418, 1))))
          + _ent(0x0007, 5, 3, struct.pack("<I", tm))
          + _ent(0x001D, 2, 11, struct.pack("<I", gd)) + struct.pack("<I", 0))
    tiff = b"II*\x00" + struct.pack("<I", o0) + i0 + ex + gp + bytes(pool)
    app1 = b"Exif\x00\x00" + tiff
    jpg = (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
           + b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 2304, 4096, 3) + b"\x00" * 9
           + b"\xff\xd9")
    pj = tmp_db.with_suffix(".jpg")
    pj.write_bytes(jpg)
    got = PH.read(pj)
    assert abs(got["lat"] - lat0) < 1e-6 and abs(got["lon"] - lon0) < 1e-6, got
    assert got["t_source"] == "gps" and got["orientation"] == 6, got
    mt = PH.match([got], rt, t0_ms=rt["t0"], max_m=500)
    assert len(mt["points"]) == 1, mt["loose"]

    # 时区兜底：相机把本地时间当 UTC 写（差整 8 小时）也要能挂上
    skew = dict(got, t_utc=got["t_utc"] + 8 * 3600 * 1000)
    m2 = PH.match([skew], rt, t0_ms=rt["t0"], max_m=500)
    assert len(m2["points"]) == 1 and m2["points"][0]["t_source"].endswith("+8h"), m2
    # 没有坐标：时间本身就定位了位置，标 approx 而不是丢掉
    nogps = dict(got, lat=None, lon=None)
    m3 = PH.match([nogps], rt, t0_ms=rt["t0"])
    assert len(m3["points"]) == 1 and m3["points"][0]["approx"] is True, m3
    # 单文件实况：JPEG 尾部内嵌 MP4，能按 ftyp 定位并整段切出
    from garmin_cn import clip as CL

    box = struct.pack(">I", 24) + b"ftypisom" + b"\x00" * 16 + b"\x00" * 5000
    live = tmp_db.with_suffix(".live.jpg")
    live.write_bytes(jpg + box)
    assert CL.embedded_mp4(live) == (len(jpg), len(box)), CL.embedded_mp4(live)
    assert CL.embedded_mp4(pj) is None            # 普通照片不能误判
    live.unlink(missing_ok=True)
    pj.unlink(missing_ok=True)

    print(f"√ 自检通过：恢复度 {res['readiness']['score']}，CTL {res['load']['ctl']}，"
          f"ACWR {res['load']['acwr']}，输出 {out.stat().st_size // 1024} KB")
    for p in (tmp_db, tmp_html):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def main() -> int:
    _setup_io()
    p = argparse.ArgumentParser(description="Garmin 国区 → 每日看板 / 电子教练")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("login", help="登录（支持邮箱验证码）")
    q.add_argument("--email")
    q.add_argument("--password")
    q.add_argument("--verbose", action="store_true", help="出错时打印完整堆栈")
    q.set_defaults(func=cmd_login)

    q = sub.add_parser("debug-login", help="登录诊断（打印每一步 HTTP）")
    q.add_argument("--email")
    q.add_argument("--password")
    q.set_defaults(func=cmd_debug_login)

    q = sub.add_parser("import-cookies", help="手动导入浏览器 Cookie")
    q.add_argument("--cookie")
    q.set_defaults(func=cmd_import_cookies)

    q = sub.add_parser("sync", help="同步数据")
    q.add_argument("--days", type=int, default=30)
    q.add_argument("--start", help="补历史的起始日 YYYY-MM-DD（与 --end 配合，会覆盖 --days）")
    q.add_argument("--end", help="区间结束日 YYYY-MM-DD，默认今天")
    q.add_argument("--activity-pages", type=int, default=None,
                   help="活动列表翻页数（每页 60 条）；补历史时默认按天数自动算")
    q.add_argument("--extras", action="store_true", help="额外拉压力/身体电量明细（更慢）")
    q.add_argument("--force", action="store_true",
                   help="整段重拉（默认增量：已稳定且拉过的日子直接跳过）")
    q.add_argument("--no-cards", action="store_true", help="不为新活动生成活动图")
    q.add_argument("--no-body", dest="body", action="store_false",
                   help="跳过体重/体成分（默认会顺手拉一次小米秤）")
    q.set_defaults(func=cmd_sync, body=True)

    q = sub.add_parser("report", help="生成每日看板")
    q.add_argument("--date")
    q.add_argument("--open", action="store_true")
    q.set_defaults(func=cmd_report)

    q = sub.add_parser("card", help="生成一图流图片看板（PNG）")
    q.add_argument("--date")
    q.add_argument("--open", action="store_true")
    q.set_defaults(func=cmd_card)

    q = sub.add_parser("meal", help="记录/查看当天饮食（热量与三大营养素）")
    q.add_argument("--date")
    q.add_argument("--add", metavar="名称", help="新增一餐，如「鸡排 250g」")
    q.add_argument("--kcal", type=float, help="这一餐的热量")
    q.add_argument("--p", type=float, help="蛋白 g")
    q.add_argument("--c", type=float, help="碳水 g")
    q.add_argument("--f", type=float, help="脂肪 g")
    q.add_argument("--time", help="时间 HH:MM，默认现在")
    q.set_defaults(func=cmd_meal)

    q = sub.add_parser("nap", help="补录日间小睡（佳明漏记时）")
    q.add_argument("--date")
    q.add_argument("--minutes", type=float, help="睡了多久（分钟）")
    q.add_argument("--time", help="入睡时间 HH:MM，默认现在")
    q.set_defaults(func=cmd_nap)

    q = sub.add_parser("sleepfix", help="矫正某晚实际睡眠时段（手表没戴/掉落时用）")
    q.add_argument("--date", help="日期（睡眠记在哪天就填哪天，通常=醒来那天）")
    q.add_argument("--start", help="实际入睡 HH:MM")
    q.add_argument("--end", help="实际醒来 HH:MM")
    q.add_argument("--note", help="备注，如「手表掉落」")
    q.set_defaults(func=cmd_sleepfix)

    q = sub.add_parser("act", help="单次活动明细与活动图")
    q.add_argument("--id", help="活动 ID")
    q.add_argument("--date", help="日期，默认取该日最长的一次活动")
    q.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    q.add_argument("--card", action="store_true", help="同时生成活动图 PNG")
    q.add_argument("--title", help="覆盖活动图标题（如赛事名）")
    q.add_argument("--privacy", choices=("none", "trim", "hide"), default="none",
                   help="分享脱敏：none 原图 / trim 首尾各裁 200m / hide 完全隐藏轨迹")
    q.add_argument("--open", action="store_true")
    q.set_defaults(func=cmd_act)

    q = sub.add_parser("race", help="赛事成绩：录官方成绩 / 看 PB / 出赛后复盘图")
    q.add_argument("--add", action="store_true", help="录入一场比赛的官方成绩")
    q.add_argument("--date", help="日期 YYYY-MM-DD")
    q.add_argument("--event", help="赛事名称，默认「比赛」")
    q.add_argument("--bib", help="参赛号")
    q.add_argument("--id", help="关联的佳明活动 ID")
    q.add_argument("--net", type=float, help="净成绩（秒），如 5301 表示 1:28:21")
    q.add_argument("--gun", type=float, help="枪声成绩（秒）")
    q.add_argument("--dist", type=float, help="官方距离（km），半马 21.0975")
    q.add_argument("--target", type=float, help="赛前目标时间（秒）")
    q.add_argument("--note", help="备注：赛道特点、天气等")
    q.add_argument("--best", action="store_true", help="列出个人最好成绩")
    q.add_argument("--card", action="store_true", help="生成赛后复盘图 PNG")
    q.add_argument("--coach", help="复盘图上的一段点评")
    q.add_argument("--open", action="store_true")
    q.set_defaults(func=cmd_race)

    q = sub.add_parser("motion", help="动态轨迹回放（1080×1920，网页或 MP4）")
    q.add_argument("--id", help="活动 ID")
    q.add_argument("--date", help="日期，默认取该日最长的一次活动")
    q.add_argument("--photos", help="照片目录，默认 data/photos/<活动ID>")
    q.add_argument("--duration", type=float, default=45.0, help="成片时长（秒），默认 45")
    q.add_argument("--out", help="输出路径（.html 或 .mp4）")
    q.add_argument("--mp4", action="store_true", help="同时导出 MP4（需要 ffmpeg）")
    q.add_argument("--fps", type=int, default=30, help="导出帧率，默认 30")
    q.add_argument("--workers", type=int, default=0,
                   help="逐帧渲染的并行浏览器数，默认按核数取（2~6）")
    q.add_argument("--keep-frames", action="store_true",
                   help="保留逐帧图片（默认在合成 MP4 成功后删掉，约 450 MB）")
    q.add_argument("--mute", action="store_true",
                   help="不混入实况片段的原声（默认保留）")
    q.add_argument("--force", action="store_true", help="忽略缓存重新抓轨迹")
    q.add_argument("--open", action="store_true")
    q.set_defaults(func=cmd_motion)

    q = sub.add_parser("ingest", help="把收件箱里的照片/视频自动归到活动并出片")
    q.add_argument("--dir", help="收件箱目录，默认 data/inbox")
    q.add_argument("--duration", type=float, default=45.0, help="成片时长（秒），默认 45")
    q.add_argument("--fps", type=int, default=30)
    q.add_argument("--workers", type=int, default=0)
    q.add_argument("--keep-frames", action="store_true")
    q.add_argument("--mute", action="store_true", help="不混入实况原声")
    q.add_argument("--html-only", action="store_true", help="只出网页，不导 MP4")
    q.add_argument("--dry-run", action="store_true", help="只报告归组结果，不动文件")
    q.set_defaults(func=cmd_ingest)

    q = sub.add_parser("photo", help="检查照片 EXIF 与轨迹匹配情况")
    q.add_argument("--dir", help="照片目录，默认 data/photos")
    q.add_argument("--id", help="用哪次活动做匹配检查")
    q.add_argument("--date", help="按日期选活动")
    q.set_defaults(func=cmd_photo)

    q = sub.add_parser("backfill", help="批量补齐历史活动明细")
    q.add_argument("--limit", type=int, default=0)
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=cmd_backfill)

    q = sub.add_parser("coach", help="输出教练上下文（Markdown）")
    q.add_argument("--date")
    q.set_defaults(func=cmd_coach)

    q = sub.add_parser("plan", help="读取佳明日历排课（教练计划兜底）与本周课表")
    q.add_argument("--date")
    q.add_argument("--cached", action="store_true", help="只读本地快照，不联网")
    q.add_argument("--diag", action="store_true",
                   help="诊断：打印日历月份、条数、类型分布与样例")
    q.set_defaults(func=cmd_plan)

    q = sub.add_parser("today", help="今日 105K 备赛课次（跑 today --week 看整周）")
    q.add_argument("--date", help="日期，默认今天")
    q.add_argument("--week", action="store_true", help="打印当天所在整周")
    q.add_argument("--days", type=int, default=2, help="顺带预览未来几天，默认 2")
    q.add_argument("--json", action="store_true", help="输出 JSON（给自动化用）")
    q.set_defaults(func=cmd_today)

    q = sub.add_parser("push", help="把 105K 课表推成佳明训练课程（--dry-run 先看结构）")
    q.add_argument("--date", help="只推这一天")
    q.add_argument("--week", type=int, help="只推第 N 周")
    q.add_argument("--start", help="起始日期")
    q.add_argument("--end", help="结束日期")
    q.add_argument("--all", action="store_true", help="推整个 21 周")
    q.add_argument("--dry-run", action="store_true", help="只生成 JSON，不联网提交")
    q.add_argument("--verbose", action="store_true", help="演练时打印完整 JSON")
    q.add_argument("--replace", action="store_true",
                   help="先删除本计划推送过的旧课程（课名以 W<周数> 开头），再推新版")
    q.set_defaults(func=cmd_push)

    q = sub.add_parser("note", help="写入教练点评（看板会显示）")
    q.add_argument("--date")
    q.add_argument("--text")
    q.add_argument("--file", help="从文件读取点评（推荐，避免转义问题）")
    q.set_defaults(func=cmd_note)

    q = sub.add_parser("body", help="体重 / 体成分：导入、趋势、与负荷的联合判断")
    q.add_argument("--import", dest="import_file", metavar="文件",
                   help="导入 CSV/JSON（小米运动健康数据副本、Zepp Life 导出）")
    q.add_argument("--source", help="标记来源，默认 csv（mifitness/zepp/ble）")
    q.add_argument("--garmin", action="store_true", help="从佳明拉取 weigh-in")
    q.add_argument("--mi", action="store_true", help="从小米健康云拉体脂秤数据")
    q.add_argument("--mi-days", type=int, default=120, help="往小米云端回溯多少天，默认 120")
    q.add_argument("--date", help="截止日期")
    q.add_argument("--days", type=int, default=28, help="看多少天，默认 28")
    q.set_defaults(func=cmd_body)

    q = sub.add_parser("mi", help="小米健康云：登录 / 体检（体脂秤数据源）")
    q.add_argument("action", nargs="?", default="status",
                   choices=["login", "status", "probe", "logout"],
                   help="默认 status；probe 额外打印云端原始字段")
    q.add_argument("--user", help="小米账号（手机号 / 邮箱 / 小米 ID）")
    q.add_argument("--password", help="密码（不推荐写在命令行历史里；不给会交互输入）")
    q.add_argument("--cookie", help="浏览器里复制的整串 Cookie（含 serviceToken 或 passToken）")
    q.add_argument("--cookie-file", metavar="文件",
                   help="从文件读 Cookie（PowerShell 引号麻烦时用这个）")
    q.add_argument("--json-file", metavar="文件",
                   help="登录材料文件：serviceLogin 的 JSON，或 F12 里复制的响应头整段")
    q.add_argument("--json-text", help="同 --json-file，直接给 JSON 文本")
    q.add_argument("--raw", action="store_true", help="打印云端原始字段")
    q.set_defaults(func=cmd_mi)

    q = sub.add_parser("coros", help="高驰：登录 / 状态 / 同步（数据源）")
    q.add_argument("action", nargs="?", default="status",
                   choices=["login", "status", "sync", "logout"])
    q.add_argument("--user", help="高驰账号邮箱（sync 时带上可自动重登）")
    q.add_argument("--password", help="密码（不推荐写在命令行历史里）")
    q.add_argument("--region", choices=["asia", "eu", "us"], default="asia",
                   help="账号所在区，默认 asia（国内）")
    q.add_argument("--days", type=int, default=30, help="同步回溯天数，默认 30")
    q.set_defaults(func=cmd_coros)

    q = sub.add_parser("nutri", help="今天吃多少：热量与三大营养素目标（减脂/维持/增重）")
    q.add_argument("--date")
    q.add_argument("--goal", choices=["cut", "maintain", "gain"],
                   help="设置身体目标并保存（cut 减脂 / maintain 维持 / gain 增重）")
    q.set_defaults(func=cmd_nutri)

    q = sub.add_parser("brief", help="把最近一次跑步压成可发微信的短消息")
    q.add_argument("--id", help="活动 ID")
    q.add_argument("--date", help="日期，默认取最近一次")
    q.add_argument("--last", type=int, default=1, help="取最近几条，默认 1")
    q.add_argument("--any", action="store_true", help="不限运动类型")
    q.add_argument("--no-card", action="store_true", help="不生成活动图")
    q.add_argument("--json", action="store_true", help="输出 JSON（给机器人/自动化用）")
    q.add_argument("--open", action="store_true")
    q.set_defaults(func=cmd_brief)

    q = sub.add_parser("watch", help="检查新跑完的活动并出简报（定时任务用）")
    q.add_argument("--hours", type=int, default=36, help="往前找多少小时，默认 36")
    q.add_argument("--no-sync", action="store_true", help="不联网同步，只用库里数据")
    q.add_argument("--no-card", action="store_true", help="不生成活动图")
    q.add_argument("--dry-run", action="store_true", help="只看会推什么，不写已推送标记")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_watch)

    q = sub.add_parser("status", help="查看状态")
    q.set_defaults(func=cmd_status)

    q = sub.add_parser("doctor", help="环境自检（排查跑不通）")
    q.set_defaults(func=cmd_doctor)

    q = sub.add_parser("key", help="配置/检查高德底图 Key")
    q.add_argument("--set", metavar="KEY", help="写入 data/amap.json 并立即探活")
    q.add_argument("--clear", action="store_true", help="删除已保存的 Key")
    q.set_defaults(func=cmd_key)

    q = sub.add_parser("serve", help="手机看板：把 output/ 只读挂到局域网")
    q.add_argument("--lan", action="store_true",
                   help="绑 0.0.0.0 供手机访问（会自动生成访问码）")
    q.add_argument("--port", type=int, default=8765, help="端口，默认 8765（占用则自动后移）")
    q.add_argument("--token", help="自定义访问码（默认随机）")
    q.add_argument("--open", action="store_true", help="同时在电脑上打开")
    q.set_defaults(func=cmd_serve)

    q = sub.add_parser("ai", help="AI 教练：对话 / 今日建议 / 体重分析 / 模型服务配置")
    q.add_argument("--port", type=int, default=8765, help="端口，默认 8765（占用则自动后移）")
    q.add_argument("--model", choices=["mock"], help="用本地假模型调试界面，不花钱")
    q.add_argument("--mock-port", type=int, default=18099, help="mock 模型端口")
    q.set_defaults(func=cmd_ai)

    q = sub.add_parser("demo", help="用仿真数据预览看板")
    q.add_argument("--open", action="store_true")
    q.add_argument("--fresh", action="store_true", help="重建仿真数据（会清掉已有示例点评）")
    q.set_defaults(func=cmd_demo)

    q = sub.add_parser("selftest", help="离线自检")
    q.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as e:
        print(f"\n× 出错了：{type(e).__name__}: {e}")
        print("  排查：run.bat doctor")
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
