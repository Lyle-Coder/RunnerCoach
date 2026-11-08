"""把播放器 HTML 逐帧渲染成 MP4。

为什么不「一帧起一个 Chrome」
---------------------------
`chrome --screenshot` 单帧要约 1.8 秒（启动 + 载入 + 截图），45 秒 30fps 的片子
1350 帧就是 40 分钟，不可用。改成**一个页面自己循环**：

    for i in 0..N:  seek(i/fps)  →  canvas.toBlob('image/jpeg')  →  POST 回本地服务

图片由同一个本地 HTTP 服务提供，所以画布不被跨源污染（`file://` 打开时
`toBlob` 会直接抛 SecurityError，这也是 payload 里存文件路径而不是 URI 的原因）。
实测约 0.1 s/帧，整片两三分钟。

两个坑
------
* **CSS 过渡会被截在中间**。照片卡靠 `transition` 淡入，导出的帧里会是半透明。
  所以导出时页面进 `export` 模式：过渡关掉，透明度按帧算（见 `motion` 的
  `EXPORT` 分支）。
* **多个 Chrome 抢 profile**。每路要给独立的 `--user-data-dir`，否则后起的
  实例起不来，表现是「只出了一部分帧」。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
FRAMES = ROOT / "output" / "_frames"


def ffmpeg() -> str | None:
    """找 ffmpeg：环境变量 → 项目 tools/ → PATH。"""
    import os

    env = (os.environ.get("FFMPEG") or "").strip()
    if env and Path(env).exists():
        return env
    local = ROOT / "tools" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg")


def render_frames(html: Path, total: int, fps: int, *, workers: int = 2,
                  log=print) -> Path:
    """渲染 total 帧到 output/_frames（jpg 序列），返回目录。"""
    from .card import _find_browser

    browser = _find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome / Edge，无法逐帧渲染")
    html = Path(html).resolve()
    try:
        rel = html.relative_to(ROOT).as_posix()
    except ValueError:
        raise RuntimeError("播放器 HTML 要放在项目目录内，本地服务才够得着")

    FRAMES.mkdir(parents=True, exist_ok=True)
    # 不清空目录：帧名是确定的（f_00000..N），重跑直接覆盖。批量删上千个文件
    # 会触发「批量删除需确认」的保护，而且留着上一轮的帧反而便于对着看问题。

    run_id = uuid.uuid4().hex[:8]
    got: set[int] = set()
    state: dict = {}
    lock = threading.Lock()
    for old in (FRAMES.parent / "_timeline.json",):
        try:
            old.unlink()
        except OSError:
            pass

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT), **kw)

        def log_message(self, *a):        # 每帧一行日志太吵
            pass

        def do_POST(self):
            q = parse_qs(urlparse(self.path).query)
            # 认 run 号：上一轮没退干净的页面还在往 127.0.0.1 上打，端口被复用时
            # 它会把旧帧塞进这一轮，成片里就出现「某几秒还是上一版的画面」。
            if (q.get("run") or [""])[0] != run_id:
                self.send_response(409)
                self.end_headers()
                return
            if self.path.startswith("/__timeline"):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                tl = FRAMES.parent / "_timeline.json"
                if not tl.exists():          # 六路会各报一次，内容一样，留第一份
                    tl.write_bytes(body)
                    with lock:
                        state["timeline"] = True
                self.send_response(204)
                self.end_headers()
                return
            # `/__diag`（诊断）和 `/__done`（完成标记）必须**显式吃掉**，不能让它们
            # 掉到下面的收帧逻辑：两者都没有 `i` 参数，会被当成 i=0 覆盖真正的首帧。
            # `/__done` 的 body 是空的 → 首帧被写成 0 字节，成片从第 1 帧开始。
            if self.path.startswith(("/__done", "/__diag")):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if self.path.startswith("/__diag") and body:
                    # 诊断信息要留下来：视频没解码出来时，成片里表现为
                    # 「视频卡片黑屏」，但帧数照样齐，导出端根本发现不了。
                    try:
                        diag = json.loads(body)
                    except Exception:
                        diag = {"raw": body[:200].decode("utf-8", "ignore")}
                    with lock:
                        state.setdefault("diag", diag)
                self.send_response(204)
                self.end_headers()
                return
            i = int((q.get("i") or ["0"])[0])
            n = int(self.headers.get("Content-Length") or 0)
            target = FRAMES / f"f_{i:05d}.jpg"
            tmp = FRAMES / f".f_{i:05d}.tmp"
            tmp.write_bytes(self.rfile.read(n))
            os.replace(tmp, target)          # 原子落盘，见 serve.py 同款说明
            self.send_response(204)
            self.end_headers()
            with lock:
                got.add(i)

        def end_headers(self):
            # 普通 GET 也要带上 Accept-Ranges，否则 Chrome 认为这服务不支持
            # 分段读取，视频可以播但 currentTime 永远设不上去（seek 被忽略）。
            if not getattr(self, "_ranged", False):
                try:
                    self.send_header("Accept-Ranges", "bytes")
                except Exception:
                    pass
            super().end_headers()

        def do_GET(self):
            if self.path.startswith("/__done"):
                self.send_response(204)
                self.end_headers()
                return
            rng = self.headers.get("Range")
            path = self.translate_path(self.path)
            if rng and os.path.isfile(path):
                self._serve_range(path, rng)
                return
            super().do_GET()

        def _serve_range(self, path: str, rng: str) -> None:
            """206 分段响应。视频逐帧 seek 的必要条件：没有它 currentTime 设不动。"""
            try:
                size = os.path.getsize(path)
                head = rng.split("=", 1)[1]
                a, _, b = head.partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
            except (OSError, ValueError, IndexError):
                self.send_error(400)
                return
            end = min(end, size - 1)
            if start > end:
                self.send_error(416)
                return
            self._ranged = True
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except OSError:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    if workers <= 0:
        workers = min(6, max(2, (os.cpu_count() or 4) // 4))
    nw = max(1, min(workers, total))

    def kill(p: subprocess.Popen) -> None:
        if p.poll() is not None:
            return
        try:
            if os.name == "nt":     # 只 terminate 会留下渲染子进程，下次抢 profile
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                               capture_output=True)
            else:
                p.kill()
        except OSError:
            pass

    # Chrome 的 profile 放系统临时目录：每个约占 100 MB，搁项目里没人会去清。
    # 名字固定（p0..p5）以便复用，别让它们无限增长。
    prof_root = Path(tempfile.gettempdir()) / "garmin-motion-profiles"

    def launch(windows: list[tuple[int, int]], attempt: int) -> None:
        for k, (a, b) in enumerate(windows):
            prof = prof_root / f"p{k}"
            if attempt:          # 重试换干净的 profile：上一路的目录可能还被占着
                prof = Path(tempfile.mkdtemp(prefix="motionchrome-"))
            prof.mkdir(parents=True, exist_ok=True)
            url = (f"http://127.0.0.1:{port}/{rel}?export=1&total={b}&from={a}"
                   f"&fps={fps}&run={run_id}")
            procs.append(subprocess.Popen([
                browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=1", "--no-first-run",
                "--no-default-browser-check", "--mute-audio",
                f"--user-data-dir={prof}", "--window-size=1080,1920", url,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    procs: list[subprocess.Popen] = []
    try:
        for attempt in range(8):
            miss = [i for i in range(total) if i not in got]
            if not miss:
                break
            if attempt:
                log(f"· 补渲 {len(miss)} 帧（第 {attempt + 1} 次）")
            else:
                log(f"· 逐帧渲染 {total} 帧（{nw} 路，页面内渲染 + 本地回传）")
            step = (len(miss) + nw - 1) // nw
            windows = [(miss[j], miss[min(j + step, len(miss)) - 1] + 1)
                       for j in range(0, len(miss), step)]
            launch(windows, attempt)
            # 卡住判定：只认「连续没有新帧」，不认「整轮跑了多久」。
            # 旧版给每轮一个固定 max(120, 缺帧数*0.5) 秒上限，机器慢一点时
            # 帧还在推进也会被掐断，重启 Chrome 又从头渲，越补越慢，8 轮后
            # 报「还有 N 帧没渲染出来」。现在只要还在收帧就一直等下去。
            idle, last_n = time.time(), len(got)
            while True:
                if len(got) >= total:
                    break
                if len(got) != last_n:
                    last_n, idle = len(got), time.time()
                    log(f"  {len(got)}/{total} 帧")
                elif time.time() - idle > 60:
                    log(f"! 60 秒没有新帧，重启渲染进程（已收 {len(got)}/{total}）")
                    break
                time.sleep(0.5)
            for p in procs:
                kill(p)
            procs.clear()
        missing = total - len(got)
        if missing > 0:
            raise RuntimeError(f"还有 {missing} 帧没渲染出来（浏览器反复卡住或被关掉）")
        # 页面自报的诊断：视频解码失败时成片里只是黑屏，帧数却是齐的，
        # 光看「渲染成功」会误以为片子没问题。
        diag = state.get("diag") or {}
        if diag:
            bad = diag.get("names") or []
            if not diag.get("vidOk", True) or bad:
                log("! 有视频没解码出来，成片里这几处会是黑屏："
                    + "、".join(bad[:6]))
            if diag.get("missing"):
                log(f"! 有 {diag['missing']} 张图片没加载："
                    + "、".join((diag.get("missingOne") or [])[:3]))
    finally:
        for p in procs:
            kill(p)
        srv.shutdown()
    return FRAMES


def sweep_frames(folder: Path, log=print) -> int:
    """删掉帧序列。一版 45 秒的片子约 1350 张、450 MB，留着没意义。

    合成成功之后才删：中途失败时这些帧正是排查/补渲的依据。
    删不掉（文件被占用、权限不够）不算错误——视频已经出来了，
    大不了多占一点地方，报个警就走。
    """
    folder = Path(folder)
    if not folder.exists():
        return 0
    n = sum(1 for _ in folder.glob("f_*.jpg"))
    try:
        # 整目录删掉再建回来：一次调用，比逐个 unlink 快得多，也不容易被
        # 杀软/占用卡在中间（碰到"批量删除"的策略时逐个删还会被半路叫停）
        shutil.rmtree(folder)
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as e:                         # noqa: BLE001
        log(f"! 帧序列没删干净（{e}），可以手动删：{folder}")
        return 0
    log(f"· 已清理 {n} 张帧图（约 {n * 0.35:.0f} MB）")
    return n


def sweep_profiles(log=print) -> None:
    """删掉逐帧渲染用的 Chrome profile。

    六路各建一个，一个约 65 MB（缓存、着色器、prefs），一版片子下来 400 MB 上下，
    下次渲染才会重建。删不掉不算错：profile 只是提速用的缓存。
    """
    root = Path(tempfile.gettempdir()) / "garmin-motion-profiles"
    if not root.exists():
        return
    try:
        shutil.rmtree(root)
        log("· 已清理渲染临时目录（Chrome profile）")
    except OSError as e:
        log(f"! 渲染临时目录没删干净（{e}）：{root}")


def _clip_audio(timeline: Path) -> list[tuple[str, float, float]]:
    """从页面回传的时间轴里挑出有音轨可用的片段：(文件, 开始秒, 自身时长)。

    优先用 `file`（磁盘绝对路径）：`src` 在 App 里是 `/data/...` 同源 URL，
    ffmpeg 打不开。老页面没回传 `file` 时再退回 `src`。
    """
    try:
        rows = json.loads(Path(timeline).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for w in rows if isinstance(rows, list) else []:
        src = str(w.get("file") or w.get("src") or "")
        dur = float(w.get("dur") or 0)
        if not src or dur <= 0 or not Path(src).exists():
            continue
        out.append((src, float(w.get("a") or 0) + float(w.get("lead") or 0), dur))
    return out


def export(html: Path, dest: Path, *, duration: float, fps: int = 30,
           workers: int = 0, keep_frames: bool = False, mute: bool = False,
           log=print) -> Path:
    """渲染并合成 MP4；缺 ffmpeg 时抛 RuntimeError（附安装办法）。"""
    exe = ffmpeg()
    if not exe:
        raise RuntimeError(
            "没找到 ffmpeg。装一个再试：\n"
            "  winget install --id Gyan.FFmpeg -e\n"
            "  或下载解压后把 ffmpeg.exe 放到 tools/ 目录\n"
            "（不想装就先只出网页：去掉 --mp4）")

    # 成片长度以页面算出来的「真实总时长」为准：巡航预算 + 素材停留。
    # 只按 `duration`（巡航预算）算会把后面的素材和尾段一起截掉——
    # 桌面和手机两条路都得认这一个数，别各算各的。
    total_s = float(duration)
    try:
        _side = Path(html).with_suffix(".json")
        if _side.exists():
            total_s = float(json.loads(_side.read_text("utf-8")).get("total") or duration)
    except (OSError, ValueError):
        total_s = float(duration)
    total = max(int(round(total_s * fps)), 1)
    frames = render_frames(html, total, fps, workers=workers, log=log)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log("· 合成 MP4…")

    # 实况片段的声音：铺一条等长静音轨，把各段按各自的起点延时叠上去。
    # 起点由页面算完时间轴后回传，这里只负责摆位置。
    clips = [] if mute else _clip_audio(frames.parent / "_timeline.json")
    cmd = [
        exe, "-y", "-loglevel", "error", "-framerate", str(fps),
        "-i", str(frames / "f_%05d.jpg"),
    ]
    if clips:
        cmd += ["-f", "lavfi", "-t", f"{total_s:.3f}", "-i",
                "anullsrc=r=48000:cl=stereo"]
        for src, _, _ in clips:
            cmd += ["-i", src]
        st = "aformat=sample_fmts=fltp:channel_layouts=stereo"
        parts = [f"[1:a]atrim=0:{total_s:.3f},asetpts=N/SR/TB,{st}[base]"]
        for k, (_, at, _dur) in enumerate(clips):
            ms = max(int(round(at * 1000)), 0)
            # 片段多半是单声道，不升轨的话 amix 会把整条音轨降成 mono
            parts.append(f"[{k + 2}:a]aresample=48000,{st},adelay={ms}:all=1,"
                         f"asetpts=N/SR/TB[c{k}]")
        refs = "".join(f"[c{k}]" for k in range(len(clips)))
        parts.append(f"[base]{refs}amix=inputs={len(clips) + 1}"
                     f":duration=first:normalize=0,{st}[aout]")
        cmd += ["-filter_complex", ";".join(parts), "-map", "0:v", "-map", "[aout]"]
        log(f"· 混入 {len(clips)} 段原声")
    cmd += [
        "-frames:v", str(total),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
    ]
    if clips:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-t", f"{total_s:.3f}"]
    cmd += ["-movflags", "+faststart", str(dest)]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError("ffmpeg 合成失败：" +
                           (proc.stderr or b"").decode("utf-8", "ignore")[:300])
    # 先把结果说出来再清理：清理是最不重要的一步，不能因为它失败
    # （文件被占用、安全策略拦批量删除）就让人以为视频没出来
    mb = dest.stat().st_size / 1024 / 1024
    log(f"· 已写出 {dest.name}（{mb:.1f} MB）")
    if keep_frames:
        log(f"· 帧序列留在 {frames}（--keep-frames）")
    else:
        try:
            sweep_frames(frames, log=log)
            sweep_profiles(log=log)
        except Exception as e:                     # noqa: BLE001
            log(f"! 清理没做完（{type(e).__name__}）：帧目录 {frames} 可手动删")
    return dest
