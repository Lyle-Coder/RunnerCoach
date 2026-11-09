"""RunnerCoach 发布助手：版本校验、打包、打 Tag、上传 Gitee Release。

纯标准库实现，不引第三方依赖；凭据只从 GITEE_TOKEN 环境变量读，绝不落盘、
不进产物、不进日志。除 upload 外全部命令离线可用。

用法：
    py -3 tools/release.py status
    py -3 tools/release.py check
    py -3 tools/release.py prepare
    py -3 tools/release.py tag --push
    py -3 tools/release.py upload --tag v0.1.5 [--dry-run]
    py -3 tools/release.py selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
INIT = ROOT / "garmin_cn" / "__init__.py"
GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
API = "https://gitee.com/api/v5"
RELEASE_PAGE = "https://gitee.com/liyufei-lyle/RunnerCoach/releases"
TOKEN_ENV = "GITEE_TOKEN"
FALLBACK_SLUG = ("liyufei-lyle", "RunnerCoach")
MAX_ASSET = 100 * 1024 * 1024  # Gitee 单个 Release 附件上限约 100 MB

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)

# 双保险：即使有人误把这些文件加进版本控制，打包时也不会带出去。
SENSITIVE_EXACT = {
    "config.json", ".github_token", "session.json", "mi_auth.json",
    "mi_login.json", "mi_cookie.txt", "captcha.jpg",
}
SENSITIVE_PATHS = {"tools/make_plan.py", "tools/make_card.py"}
SENSITIVE_PREFIX = ("data/", "output/", "dist/", "site/", ".git/")
SENSITIVE_SUFFIX = (".db", ".db-journal", ".apk", ".aab", ".log", ".pyc", ".genie")

# 不参与分发的目录：留在仓库里自己用，但不进源码包。
# server/ 是一套**未启用**的服务端（短信/支付/订单/中转），与「数据不出本机」
# 的产品定位无关；混进源码包会让人以为这是产品的一部分，也让包变大。
PACK_EXCLUDE_PREFIX = ("server/",)

APK_SOURCES = (
    ("debug", ROOT / "android/app/build/outputs/apk/debug", "app-debug*.apk"),
    ("release", ROOT / "android/app/build/outputs/apk/release", "app-release*.apk"),
)


class ReleaseError(Exception):
    """可预期、需要直接展示给用户的发布错误。"""


# ── 基础设施 ────────────────────────────────────────────────────────────


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def git(*args: str, check: bool = True) -> str:
    p = run(["git", "-C", str(ROOT), *args])
    if check and p.returncode:
        raise ReleaseError(f"git {' '.join(args)} 失败：{(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def redact(text: str, *secrets_: str) -> str:
    for s in secrets_:
        if s:
            text = text.replace(s, "***")
    return text


def current_version() -> str:
    if not INIT.is_file():
        raise ReleaseError(f"找不到版本文件：{INIT}")
    m = VERSION_RE.search(INIT.read_text(encoding="utf-8"))
    if not m or not SEMVER.fullmatch(m.group(1)):
        raise ReleaseError("garmin_cn/__init__.py 里的 __version__ 不是合法的 x.y.z 版本号")
    return m.group(1)


def tag_for(version: str) -> str:
    return "v" + version


def parse_remote(url: str) -> tuple[str, str] | None:
    """同时接受 SSH 与 HTTPS 形式的 Gitee remote。"""
    m = re.search(r"gitee\.com[:/]([^/]+)/([^/?#]+?)(?:\.git)?/?$", url.strip())
    return (m.group(1), m.group(2)) if m else None


def repo_slug() -> tuple[str, str]:
    url = git("remote", "get-url", "origin", check=False)
    return parse_remote(url) or FALLBACK_SLUG


def gitee_remote() -> str:
    """指向 Gitee 的 remote 名字；都没有就退回 origin。

    同时挂着 GitHub origin 与 Gitee 备份的仓库很常见，而 Release 只发 Gitee，
    所以校验 tag 是否存在时也得看 Gitee 那头的 remote，不能写死 origin。
    """
    for name in (git("remote", check=False) or "").split():
        if parse_remote(git("remote", "get-url", name, check=False)):
            return name
    return "origin"


def is_sensitive(rel: str) -> bool:
    rel = rel.replace("\\", "/").lower()
    base = rel.rsplit("/", 1)[-1]
    return (
        base in SENSITIVE_EXACT
        or rel in SENSITIVE_PATHS
        or rel.startswith(SENSITIVE_PREFIX)
        or rel.endswith(SENSITIVE_SUFFIX)
    )


def tracked_files() -> list[str]:
    files = [f.replace("\\", "/") for f in git("ls-files", "-z").split("\0") if f]
    bad = [f for f in files if is_sensitive(f)]
    if bad:
        raise ReleaseError("版本库里出现敏感路径，先处理再发版：\n  " + "\n  ".join(bad[:10]))
    return files


def packed_files() -> list[str]:
    """真正进源码包的文件：跟踪文件里去掉不参与分发的目录。"""
    files = tracked_files()
    return [f for f in files
            if not any(f.startswith(p) for p in PACK_EXCLUDE_PREFIX)]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


def preflight(version: str) -> list[str]:
    """发布前置校验：分支、工作区、与远端同步、版本合法性。只读、离线。"""
    lines: list[str] = [f"版本 {version}（tag {tag_for(version)}）"]
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise ReleaseError(f"必须在 main 分支发版（当前 {branch}）")
    lines.append(f"√ 分支 {branch}")
    if git("status", "--porcelain"):
        raise ReleaseError("工作区有未提交改动，先提交并推送后再发版")
    lines.append("√ 工作区干净")
    head = git("rev-parse", "HEAD")
    upstream = git("rev-parse", "@{u}", check=False)
    if not upstream or upstream != head:
        raise ReleaseError("本地 HEAD 与 origin/main 不一致，先 push 或 pull --ff-only")
    lines.append("√ 与 origin/main 同步")
    files = tracked_files()
    lines.append(f"√ {len(files)} 个跟踪文件，未发现敏感路径")
    dropped = [p for p in PACK_EXCLUDE_PREFIX
               if any(f.startswith(p) for f in files)]
    if dropped:
        lines.append("· 不进源码包：" + "、".join(dropped) + "（未启用，与 App 无关）")
    lines.extend(signing_lines(version))
    return lines


# ── 正式签名 ────────────────────────────────────────────────────────────

SIGN_KEYS = ("rc.storeFile", "rc.storePassword", "rc.keyAlias")


def _local_props() -> dict[str, str]:
    """读 android/local.properties。只取 rc.* 四行，不回显口令。"""
    props: dict[str, str] = {}
    f = ROOT / "android" / "local.properties"
    if not f.is_file():
        return props
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        props[k.strip()] = v.strip()
    return props


def signing_state() -> tuple[bool, str]:
    """正式签名是否就绪。

    只看 local.properties 有没有四要素、keystore 在不在，不去解包验证证书
    （那要口令，脚本不碰口令）。真正的签名结果留给 apksigner 核对。
    """
    props = _local_props()
    missing = [k for k in SIGN_KEYS if not props.get(k)]
    if missing:
        return False, "local.properties 缺 " + "、".join(missing)
    p = Path(props["rc.storeFile"])
    if not p.is_absolute():
        p = (ROOT / "android" / p).resolve()
    if not p.is_file():
        return False, f"keystore 不存在：{p}"
    if p.suffix.lower() not in (".jks", ".keystore"):
        return False, f"keystore 后缀不像密钥库：{p.name}"
    return True, f"{p.name}（别名 {props['rc.keyAlias']}）"


def is_release_version(version: str) -> bool:
    """是不是「正式版」：不带 -dev/-snapshot/-alpha/-beta/-rc 后缀的都算。

    以前按「主版本号 >= 1」判，但 0.6.0 也可以是第一个对外正式版 ——
    决定要不要正式签名的不是数字大小，而是「会不会发给别人用」。
    """
    v = str(version or "").strip().lower()
    return not any(v.endswith(s) for s in
                   ("-dev", "-snapshot", "-alpha", "-beta", "-rc"))


def signing_lines(version: str) -> list[str]:
    """签名检查：正式版必须正式签名，开发版只提示。

    debug 签名每台机器不同、还带有效期，换签名后老用户无法覆盖安装，
    而 App 的 allowBackup=false —— 等于历史全丢。所以这条不能只是建议。
    """
    ok, detail = signing_state()
    if ok:
        return [f"√ 正式签名：{detail}"]
    if is_release_version(version):
        raise ReleaseError(
            f"正式版必须用正式 keystore 签名，当前未就绪（{detail}）。\n"
            "  在 android/local.properties 填 rc.storeFile / rc.storePassword /\n"
            "  rc.keyAlias / rc.keyPassword 四行，详见 docs/发布流程.md"
        )
    return [f"! 仍是 debug 签名（{detail}）—— 只用于自己的开发快照，别发给别人"]


def apk_version() -> str:
    if not GRADLE.is_file():
        return "未配置"
    text = GRADLE.read_text(encoding="utf-8")
    name = re.search(r'versionName\s*=\s*"([^"]+)"', text)
    code = re.search(r"versionCode\s*=\s*(\d+)", text)
    return f"{name.group(1)}（versionCode {code.group(1)}）" if name and code else "未配置"


# ── 本地产物 ────────────────────────────────────────────────────────────


def build_source_zip(tag: str, files: list[str]) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"RunnerCoach-{tag}-source.zip"
    ts = git("log", "-1", "--format=%ct", check=False)
    dt = datetime.fromtimestamp(int(ts), timezone.utc) if ts.isdigit() else datetime(1980, 1, 1)
    stamp = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    prefix = f"RunnerCoach-{tag}/"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            src = ROOT / rel
            if not src.is_file():
                continue
            info = zipfile.ZipInfo(prefix + rel, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if rel.endswith(".sh") else 0o644) << 16
            z.writestr(info, src.read_bytes())
    return out


def collect_apks(tag: str) -> list[Path]:
    found: list[Path] = []
    DIST.mkdir(parents=True, exist_ok=True)
    for channel, folder, pattern in APK_SOURCES:
        hits = sorted(folder.glob(pattern)) if folder.is_dir() else []
        if not hits:
            continue
        dest = DIST / f"RunnerCoach-{tag}-{channel}.apk"
        shutil.copy2(hits[0], dest)
        found.append(dest)
    return found


def build_notes(tag: str, version: str, apks: list[Path]) -> str:
    # 只认正式版本 tag（v1.2.3）。分支快照 tag 形如 snap-<分支>-<时间>，
    # 两者混在一起会让「本版变更」被截断成只剩一条 release 提交。
    prev = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*",
               check=False).strip()
    if prev == tag:
        # 已经手动打过同名 tag 时，往前退一个，别让区间变成空
        prev = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*",
                   f"{tag}^", check=False).strip()
    rng = f"{prev}..HEAD" if prev else "HEAD"
    log = git("log", "--no-merges", "--pretty=format:- %s (%h)", rng, check=False)
    if not log.strip():
        log = "- 首次发布"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assets = [f"RunnerCoach-{tag}-source.zip", "SHA256SUMS.txt"]
    assets += [p.name for p in apks]
    asset_lines = "\n".join(f"- `{n}`" for n in assets)
    apk_line = (
        "\n".join(f"- `{p.name}` — {human(p.stat().st_size)}" for p in apks)
        if apks else "本版未附 APK（APK 不进仓库，需先跑 `android/build.bat` 再重新 prepare）。"
    )
    return f"""# RunnerCoach {tag}

生成日期：{today}（发布时请核对）
Python 内核版本：`{version}`（`garmin_cn/__init__.py`）
APK 版本：{apk_version()}（`android/app/build.gradle.kts`，与仓库 tag 独立维护）

## 本版变更

{log}

> 提交信息由 git log 自动生成。按项目约定，提交信息里不应出现真实活动 ID、
> 日期、GPS 坐标或健康数值；发布前请再扫一眼，敏感内容应改写提交信息后再发。

## 产物与校验

{asset_lines}

源码包只包含 git 跟踪文件，不含 `config.json`、`data/`、`output/`、
会话与数据库等本机数据。校验方式：

```bash
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell：

```powershell
Get-FileHash -Algorithm SHA256 <文件名>
```

## 安卓包

{apk_line}

APK 通过 Release 附件或朋友间互传分发，**不进仓库**。安装前请自行核对
SHA256。活动图出图前会先询问分享脱敏档位（默认裁首尾各 200 m，也可完全隐藏
轨迹）；选「原图」或导出轨迹回放视频前，请自己再确认一次常跑路线是否会被看到。

## 说明

本项目是本地优先的个人工具，与 Garmin、小米、高德无关联；没有服务端、
没有遥测，凭据与健康数据只留在使用者自己的设备上。
"""


def write_checksums(paths: list[Path]) -> Path:
    out = DIST / "SHA256SUMS.txt"
    out.write_text(
        "".join(f"{sha256(p)}  {p.name}\n" for p in paths), encoding="utf-8", newline="\n"
    )
    return out


# ── Gitee API ───────────────────────────────────────────────────────────


def http(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
         method: str = "GET", token: str = "") -> str:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = redact(e.read().decode("utf-8", "replace")[:600], token)
        raise ReleaseError(f"Gitee API {method} 返回 {e.code}：{body}") from None
    except urllib.error.URLError as e:
        raise ReleaseError(f"Gitee API 连接失败：{e.reason}") from None


def gitee_ensure_release(owner: str, repo: str, tag: str, notes: str, token: str,
                         prerelease: bool = False, target: str = "main") -> str:
    base = f"{API}/repos/{owner}/{repo}"
    q = urllib.parse.quote(token, safe="")
    who = http(f"{API}/user?access_token={q}", token=token)
    login = json.loads(who).get("login", "?")
    print(f"√ Gitee 身份：{login}")
    try:
        # 没有这个 tag 的 Release 时，Gitee 返回的是 JSON null 而不是 404，
        # json.loads 得到 None——所以这里必须兜成空字典再取 id。
        existing = json.loads(http(f"{base}/releases/tags/{tag}?access_token={q}", token=token)) or {}
        if existing.get("id"):
            print(f"√ 复用已存在的 Release {tag}（id={existing['id']}）")
            return str(existing["id"])
    except ReleaseError:
        pass
    payload = json.dumps({
        "access_token": token,
        "tag_name": tag,
        "name": f"RunnerCoach {tag}",
        "body": notes,
        "target_commitish": target,
        "prerelease": bool(prerelease),
    }, ensure_ascii=False).encode("utf-8")
    created = json.loads(http(
        base + "/releases", data=payload, method="POST", token=token,
        headers={"Content-Type": "application/json; charset=utf-8"},
    ))
    if not created.get("id"):
        raise ReleaseError("创建 Release 成功但响应里没有 id，请到 Gitee 网页确认")
    print(f"√ 已创建 Release {tag}（id={created['id']}）")
    return str(created["id"])


def multipart_body(token: str, path: Path) -> tuple[bytes, str]:
    boundary = "----RunnerCoach" + secrets.token_hex(8)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="access_token"\r\n\r\n{token}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return head + path.read_bytes() + tail, f"multipart/form-data; boundary={boundary}"


def gitee_upload(owner: str, repo: str, release_id: str, path: Path, token: str) -> None:
    body, ctype = multipart_body(token, path)
    http(
        f"{API}/repos/{owner}/{repo}/releases/{release_id}/attach_files",
        data=body, method="POST", token=token, headers={"Content-Type": ctype},
    )
    print(f"√ 已上传 {path.name}（{human(path.stat().st_size)}）")


# ── 子命令 ──────────────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    v = current_version()
    print(f"版本      {v}（tag {tag_for(v)}）")
    print(f"APK 版本  {apk_version()}")
    ok, detail = signing_state()
    print(f"签名      {'正式：' + detail if ok else 'debug（' + detail + '）'}")
    print(f"仓库      {'/'.join(repo_slug())}")
    print(f"最近 tag  {git('describe', '--tags', '--abbrev=0', check=False) or '(无)'}")
    if DIST.is_dir():
        items = sorted(p for p in DIST.iterdir() if p.is_file())
        print(f"dist/     {len(items)} 个文件")
        for p in items:
            print(f"  {p.name}  {human(p.stat().st_size)}")
    else:
        print("dist/     尚未生成（跑 prepare）")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    for line in preflight(current_version()):
        print(line)
    print("√ 可以发版：下一步 prepare")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    v = current_version()
    tag = tag_for(v)
    for line in preflight(v):
        print(line)
    DIST.mkdir(parents=True, exist_ok=True)
    for old in DIST.glob(f"RunnerCoach-{tag}-*"):
        old.unlink()
    files = packed_files()
    zipped = build_source_zip(tag, files)
    apks = collect_apks(tag)
    notes_path = DIST / f"RELEASE_NOTES-{tag}.md"
    notes_path.write_text(build_notes(tag, v, apks), encoding="utf-8", newline="\n")
    sums = write_checksums([zipped, *apks])
    assets = [zipped, sums, *apks]
    print("√ 产物已生成：")
    for p in [zipped, notes_path, sums, *apks]:
        print(f"  {p.name}  {human(p.stat().st_size)}")
    print(f"√ SHA256（源码包）：{sha256(zipped)}")
    if not apks:
        print("· 未找到 APK；需要分发时先跑 android/build.bat，再重新 prepare")
    for p in assets:
        if p.stat().st_size > MAX_ASSET:
            print(f"! {p.name} 超过 {human(MAX_ASSET)}，Gitee 附件可能上传失败")
    print(f"  下一步：tag --push，然后 upload --tag {tag}")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    v = current_version()
    tag = tag_for(v)
    preflight(v)
    if not git("tag", "-l", tag):
        git("tag", "-a", tag, "-m", f"Release {tag}")
        print(f"√ 本地已创建附注标签 {tag}")
    else:
        print(f"· 本地已存在 {tag}，跳过创建")
    if args.push:
        remote = gitee_remote()
        git("push", remote, tag)
        print(f"√ 已推送 {tag} 到 {remote}")
    else:
        print(f"· 未推送。推送：py -3 tools/release.py tag --push")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    tag = args.tag
    if not TAG_RE.fullmatch(tag):
        raise ReleaseError(f"tag 格式应为 vX.Y.Z，收到：{tag}")
    v = current_version()
    if tag != tag_for(v):
        raise ReleaseError(f"tag {tag} 与当前版本 {v} 不一致：先改 garmin_cn/__init__.py 并重新 prepare")
    zipped = DIST / f"RunnerCoach-{tag}-source.zip"
    notes_path = DIST / f"RELEASE_NOTES-{tag}.md"
    sums = DIST / "SHA256SUMS.txt"
    apks = [p for p in sorted(DIST.glob(f"RunnerCoach-{tag}-*.apk"))]
    for p in (zipped, notes_path, sums):
        if not p.is_file():
            raise ReleaseError(f"缺少产物 {p.name}，先跑 prepare")
    assets = [zipped, sums, *apks]
    owner, repo = repo_slug()
    print(f"Release   {tag} @ {owner}/{repo}")
    print(f"附件      {', '.join(p.name for p in assets)}")
    print(f"接口      {API}/repos/{owner}/{repo}/releases")
    if args.dry_run:
        print("√ dry-run 完成：未联网、未读取凭据、未创建任何 Release")
        return 0
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ReleaseError(
            f"未设置 {TOKEN_ENV}。只在当前会话设置，不要写进文件：\n"
            "  PowerShell: $env:GITEE_TOKEN='<私人令牌>'\n"
            "  cmd       : set GITEE_TOKEN=<私人令牌>\n"
            "  POSIX     : export GITEE_TOKEN=<私人令牌>"
        )
    remote = gitee_remote()
    if not git("ls-remote", "--tags", remote, tag, check=False):
        raise ReleaseError(
            f"{remote} 远端还没有 {tag}，先跑：git push {remote} {tag}")
    release_id = gitee_ensure_release(owner, repo, tag, notes_path.read_text(encoding="utf-8"), token)
    for p in assets:
        if p.stat().st_size > MAX_ASSET:
            print(f"! 跳过 {p.name}：超过 Gitee 附件上限 {human(MAX_ASSET)}")
            continue
        gitee_upload(owner, repo, release_id, p, token)
    print(f"√ 发布完成：{RELEASE_PAGE}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """脚本自身的离线自检：不碰 git 状态、不联网、不读凭据。"""
    assert SEMVER.fullmatch("0.1.0") and SEMVER.fullmatch("1.2.3-rc.1")
    assert not SEMVER.fullmatch("0.1") and not SEMVER.fullmatch("v0.1.0")
    assert TAG_RE.fullmatch("v0.1.0") and not TAG_RE.fullmatch("0.1.0")
    assert tag_for("1.2.3") == "v1.2.3"
    assert parse_remote("git@gitee.com:liyufei-lyle/RunnerCoach.git") == FALLBACK_SLUG
    assert parse_remote("https://gitee.com/liyufei-lyle/RunnerCoach.git") == FALLBACK_SLUG
    assert parse_remote("git@github.com:a/b.git") is None
    assert is_sensitive("data/garmin.db") and is_sensitive("config.json")
    assert is_sensitive("output/latest.html") and is_sensitive("tools/make_plan.py")
    assert not is_sensitive("garmin_cn/store.py") and not is_sensitive("docs/发布流程.md")
    assert redact("token=SECRET tail", "SECRET") == "token=*** tail"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "hello.txt"
        p.write_bytes(b"hello")
        assert sha256(p) == hashlib.sha256(b"hello").hexdigest()
        body, ctype = multipart_body("TOKEN-XYZ", p)
        assert b'name="access_token"' in body and b"TOKEN-XYZ" in body
        assert b'filename="hello.txt"' in body and b"hello" in body
        assert body.endswith(b"--\r\n") and "boundary=" in ctype
    print("√ 发布脚本自检通过（版本 / remote 解析 / 敏感路径 / 脱敏 / 校验和 / multipart）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="RunnerCoach 发布助手（纯标准库）")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("status", help="查看版本与 dist 产物")
    q.set_defaults(func=cmd_status)
    q = sub.add_parser("check", help="发布前置校验（离线只读）")
    q.set_defaults(func=cmd_check)
    q = sub.add_parser("prepare", help="生成源码包 / APK / SHA256SUMS / Release Notes")
    q.set_defaults(func=cmd_prepare)
    q = sub.add_parser("tag", help="创建附注标签")
    q.add_argument("--push", action="store_true", help="同时把标签推到 origin")
    q.set_defaults(func=cmd_tag)
    q = sub.add_parser("upload", help="创建 Gitee Release 并上传附件")
    q.add_argument("--tag", required=True, help="如 v0.1.5，必须与 __version__ 一致")
    q.add_argument("--dry-run", action="store_true", help="只打印计划，不联网")
    q.set_defaults(func=cmd_upload)
    q = sub.add_parser("selftest", help="脚本自身离线自检")
    q.set_defaults(func=cmd_selftest)
    args = p.parse_args()
    try:
        return int(args.func(args))
    except ReleaseError as e:
        print(f"× {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
