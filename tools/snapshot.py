# -*- coding: utf-8 -*-
"""在开发分支上发一个**快照** Release（prerelease），不合并 main、不占正式版本号。

与 `tools/release.py` 的分工：

| 场景 | 工具 | tag | Release 类型 | 分支要求 |
|---|---|---|---|---|
| 正式发版 | `release.py` | `vX.Y.Z` | 正式 | 必须 main 且与上游同步 |
| 分支预览 | `snapshot.py` | `snap-<分支>-<时间>` | prerelease | 任意分支，工作区干净即可 |

快照的定位：给团队和自己在真机上试分支改动，**不是**可推荐给普通用户的版本。
正式版仍然只在 main 上按 `docs/发布流程.md` 发。

用法：

    py -3 tools/snapshot.py prepare     # 打包当前分支（APK 需先构建）
    py -3 tools/snapshot.py upload      # 创建 prerelease 并上传附件
    py -3 tools/snapshot.py run         # 两步一起做

APK 由 `android/build.bat assembleRelease` 产出；本脚本不代跑 Gradle，
避免在发布流程里藏一个几分钟的构建步骤。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import release as R  # noqa: E402

DIST = R.DIST
TAG_FILE = DIST / ".snapshot-tag"


def branch() -> str:
    return R.git("rev-parse", "--abbrev-ref", "HEAD")


def make_tag() -> str:
    b = branch().replace("/", "-")
    return f"snap-{b}-{dt.datetime.now().strftime('%Y%m%d-%H%M')}"


def preflight() -> None:
    if R.git("status", "--porcelain"):
        raise R.ReleaseError("工作区有未提交改动，先提交再发快照")
    print(f"√ 分支 {branch()}")
    print("√ 工作区干净")


def build_notes(tag: str) -> str:
    ver = R.current_version()
    b = branch()
    head = R.git("rev-parse", "--short", "HEAD")
    base = R.git("describe", "--tags", "--abbrev=0", check=False) or "HEAD~10"
    log = R.git("log", "--no-merges", "--pretty=format:- %s (%h)",
                f"{base}..HEAD", check=False) or "- （无新提交）"
    today = dt.date.today().isoformat()
    apk_ver = R.apk_version()
    return f"""# RunnerCoach 快照 · {ver}

> **这是 `{b}` 分支的预览包，不是正式版。** 正式版以 main 上的 `vX.Y.Z` 为准，
> 这个 Release 标记为 prerelease，随时可能被后续提交覆盖。

- 分支：`{b}`
- 提交：`{head}`
- 构建日期：{today}
- 内核版本：`{ver}`（`garmin_cn/__init__.py`）
- APK：{apk_ver}
- 快照 tag：`{tag}`

## 本分支相对上个正式版的改动

{log}

## 试的时候重点看

1. **素材裁剪（本版重点）**：「我的 → 素材片段 → 添加」，选一段视频后会弹出裁剪界面。
   - 上方是**真实视频预览**，点画面或「▶ 播放选段」循环播放选中片段；
   - 下方是**关键帧胶片条**，每格是该关键帧的真实画面，能直接看出哪一段拍的是什么；
   - 拖两端手柄选片段：**起点会自动吸附到关键帧**（从 GOP 中间切会花屏），
     终点自由；蓝色刻度就是全部关键帧位置；
   - 保存后提示里的时长是实际剪出来的长度，如果起点被吸附会额外说明。
2. **音画同步**：剪出来的片段开头声音和画面应该是对齐的。早期版本会错位，
   这版统一了时间原点，重点验证一下。
3. **放不了的视频**：HEVC 之类的视频预览会自动退回关键帧静帧，裁剪仍可正常完成。
4. **其余回归**：四个 tab 切换与返回键、今日课表、历史筛选、看板翻日期。

发现问题请直接回在分支提交或群里，不要基于这个包发版。

## 安装

覆盖安装即可：内核 `{ver}`，APK {apk_ver}，可直接覆盖正式版。安装前可核对下方 SHA256。

## 产物

- `RunnerCoach-{tag}-release.apk`
- `RunnerCoach-{tag}-source.zip`
- `SHA256SUMS.txt`

APK 不进仓库，只作为 Release 附件分发。
"""


def cmd_prepare(args: argparse.Namespace) -> int:
    preflight()
    tag = args.tag or make_tag()
    apks = R.collect_apks(tag)
    if not apks:
        raise R.ReleaseError(
            "没找到 APK：先跑 `cd android && build.bat assembleRelease`，再执行 prepare")
    files = R.tracked_files()
    zipped = R.build_source_zip(tag, files)
    notes_path = DIST / f"RELEASE_NOTES-{tag}.md"
    notes_path.write_text(build_notes(tag), encoding="utf-8", newline="\n")
    sums = R.write_checksums([zipped, *apks])
    TAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    TAG_FILE.write_text(tag, encoding="utf-8")
    print("√ 快照产物：")
    for p in (zipped, notes_path, sums, *apks):
        print(f"  {p.name}  {R.human(p.stat().st_size)}")
    print(f"  下一步：py -3 tools/snapshot.py upload --tag {tag}")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    tag = args.tag or (TAG_FILE.read_text(encoding="utf-8").strip()
                       if TAG_FILE.is_file() else "")
    if not tag.startswith("snap-"):
        raise R.ReleaseError(f"快照 tag 必须以 snap- 开头，收到：{tag or '(空)'}")
    zipped = DIST / f"RunnerCoach-{tag}-source.zip"
    notes_path = DIST / f"RELEASE_NOTES-{tag}.md"
    sums = DIST / "SHA256SUMS.txt"
    apks = sorted(DIST.glob(f"RunnerCoach-{tag}-*.apk"))
    for p in (zipped, notes_path, sums, *apks):
        if not p.is_file():
            raise R.ReleaseError(f"缺少产物 {p.name}，先跑 prepare")
    token = os.environ.get(R.TOKEN_ENV, "").strip()
    if not token:
        raise R.ReleaseError(
            f"未设置 {R.TOKEN_ENV}。只在当前会话设置，不要写进文件：\n"
            "  PowerShell: $env:GITEE_TOKEN='<私人令牌>'")
    owner, repo = R.repo_slug()
    remote = R.gitee_remote()
    print(f"快照      {tag} @ {owner}/{repo}（prerelease）")
    if not R.git("ls-remote", "--tags", remote, tag, check=False):
        if not R.git("tag", "-l", tag):
            R.git("tag", "-a", tag, "-m", f"Snapshot {tag}")
        R.git("push", remote, tag)
        print(f"√ 已推送 tag {tag} 到 {remote}")
    rid = R.gitee_ensure_release(owner, repo, tag,
                                 notes_path.read_text(encoding="utf-8"), token,
                                 prerelease=True, target=branch())
    for p in (zipped, sums, *apks):
        if p.stat().st_size > R.MAX_ASSET:
            print(f"! 跳过 {p.name}：超过附件上限")
            continue
        R.gitee_upload(owner, repo, rid, p, token)
    print(f"√ 快照发布完成：{R.RELEASE_PAGE}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    rc = cmd_prepare(args)
    if rc:
        return rc
    return cmd_upload(args)


def main() -> int:
    p = argparse.ArgumentParser(description="RunnerCoach 分支快照发布（prerelease）")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (
        ("prepare", cmd_prepare, "打包当前分支，生成 dist 产物"),
        ("upload", cmd_upload, "创建 prerelease 并上传附件"),
        ("run", cmd_run, "prepare + upload"),
    ):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--tag", help="指定快照 tag（默认按分支+时间生成）")
        q.set_defaults(func=fn)
    args = p.parse_args()
    try:
        return int(args.func(args))
    except R.ReleaseError as e:
        print(f"× {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
