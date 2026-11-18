# -*- coding: utf-8 -*-
"""把打好的 APK 发布到自己的服务器（替代 Gitee Release）。

    python tools/publish_apk.py --host 39.106.174.153
    python tools/publish_apk.py --host 1.2.3.4 --notes "修了结算页的空行"

它做三件事：
1. 从 `android/app/build.gradle.kts` 读出 versionName / versionCode；
2. 把 APK 传到服务器 `/opt/runnercoach/apk/`，顺手算 SHA256；
3. 写 `latest.json`（版本、文件名、大小、SHA256、日期、说明），
   App 打开时读的就是它。

为什么要有这一步而不是直接拷文件：客户端要判断"有没有新版本"，就得有人
告诉它版本号；APK 里虽然有版本，但从服务端解析 AndroidManifest 太重，
发布时写一份小 JSON 最省事，也顺便把 SHA256 落下来。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRADLE = ROOT / "android" / "app" / "build.gradle.kts"
DEFAULT_APK = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"

REMOTE_DIR = "/opt/runnercoach/apk"


def _gradle_versions() -> tuple[str, int]:
    text = GRADLE.read_text(encoding="utf-8")
    name = re.search(r'versionName\s*=\s*"([^"]+)"', text)
    code = re.search(r"versionCode\s*=\s*(\d+)", text)
    if not name or not code:
        raise SystemExit("× 读不到 versionName / versionCode，检查 build.gradle.kts")
    return name.group(1), int(code.group(1))


def _ssh(args: argparse.Namespace, cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", args.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         "-o", "StrictHostKeyChecking=accept-new", f"{args.user}@{args.host}", cmd],
        capture_output=True, text=True, errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description="发布 APK 到自建服务器")
    ap.add_argument("--host", required=True, help="服务器 IP")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--key", default=str(Path.home() / ".ssh" / "runnercoach_ed25519"))
    ap.add_argument("--apk", default=str(DEFAULT_APK))
    ap.add_argument("--notes", default="", help="更新说明（弹窗里显示）")
    ap.add_argument("--version", default="", help="覆盖版本号（默认读 gradle）")
    a = ap.parse_args()

    apk = Path(a.apk)
    if not apk.is_file():
        print(f"× 找不到 {apk}\n  先构建：cd android && build.bat assembleRelease")
        return 2

    version, code = _gradle_versions()
    version = a.version or version
    fname = f"RunnerCoach-v{version}-release.apk"
    data = apk.read_bytes()
    meta = {"version": version, "version_code": code, "file": fname,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "notes": a.notes, "published": time.strftime("%Y-%m-%d")}

    print(f"包     {apk.name}  {len(data) / 1048576:.1f} MB")
    print(f"版本   {version} ({code})")
    print(f"SHA256 {meta['sha256'][:32]}…")

    # 先传到 /tmp，再 sudo 挪进目标目录：目标目录属于服务账号，admin 直接 scp
    # 写不进去，而 scp 用不了 sudo。
    remote_tmp = f"/tmp/{fname}"
    print("→ 上传")
    up = subprocess.run(
        ["scp", "-i", a.key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         str(apk), f"{a.user}@{a.host}:{remote_tmp}"],
        capture_output=True, text=True, errors="replace")
    if up.returncode != 0:
        print("× 上传失败：", (up.stderr or "").strip()[:200])
        return 1

    print("→ 落盘并写 latest.json")
    payload = json.dumps(meta, ensure_ascii=False)
    cmd = (f"sudo install -d -o runnercoach -g runnercoach {REMOTE_DIR} && "
           f"sudo mv -f {remote_tmp} {REMOTE_DIR}/{fname} && "
           f"sudo chmod 644 {REMOTE_DIR}/{fname} && "
           f"printf '%s' '{payload}' | sudo tee {REMOTE_DIR}/latest.json >/dev/null")
    r = _ssh(a, cmd)
    if r.returncode != 0:
        print("× 远端写失败：", (r.stderr or r.stdout or "").strip()[:300])
        return 1

    # 顺手校验一次：服务端读到的元数据要和刚写的一致
    time.sleep(1)
    r = _ssh(a, f"curl -s http://127.0.0.1:8088/apk/ -o /dev/null -w '%{{http_code}}' ; "
                f"echo; sudo head -c 200 {REMOTE_DIR}/latest.json")
    print("→ 远端确认：", (r.stdout or "").strip()[:220])

    print()
    print("发布完成。客户端下次检查更新会读到：")
    print(f"  版本 {version} ({code})")
    print(f"  地址 http://{a.host}:8088/apk/{fname}")
    print("如果 App 里没弹更新，确认防火墙已放行 8088，且 App 里显示的版本低于这个。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
