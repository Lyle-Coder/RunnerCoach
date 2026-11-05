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
    net = Net(auth.session_path, min_interval=1.2)
    has_session = bool(net.get_cookie("JWT_WEB") or net.get_cookie("SESSIONID"))
    if not has_session:
        cfg = load_config()
        if _auto_login(cfg):
            net = Net(auth.session_path, min_interval=1.2)
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

    try:
        res = svc.sync(days=args.days, extras=args.extras, progress=progress)
    except SessionExpired:
        print("\n· 会话过期，尝试用已保存的账号自动续期…", flush=True)
        if not _auto_login(load_config()):
            print("× 需要重新登录：run.bat login")
            return 3
        client = get_client()
        svc = SyncService(client, store)
        try:
            res = svc.sync(days=args.days, extras=args.extras, progress=progress)
        except SessionExpired:
            print("\n× 续期后仍然被拒绝，请重新登录：run.bat login")
            return 3
    print()
    print(f"√ {res['range']}：{res['days']} 天，{res['activities']} 条活动")
    if not getattr(args, "no_cards", False):
        try:
            made = auto_cards(store, client, days=min(args.days, 7))
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
        _sync_body(store, days=min(max(args.days, 7), 120))
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
    print(f"  {meta['dist'] / 1000:.2f} km · {meta['dur'] // 60} 分 "
          f"{meta['dur'] % 60} 秒 · 成片 {args.duration:.0f} 秒")
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
    q.add_argument("--extras", action="store_true", help="额外拉压力/身体电量明细（更慢）")
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

    q = sub.add_parser("plan", help="读取佳明教练的训练计划与本周课表")
    q.add_argument("--date")
    q.add_argument("--cached", action="store_true", help="只读本地快照，不联网")
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
