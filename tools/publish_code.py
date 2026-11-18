"""发布代码包：把改动过的模块打成 zip 传到服务器（覆盖式更新）。

    python tools/publish_code.py --version 1.5.0-1 --notes "彩蛋文案"
    python tools/publish_code.py --version 1.5.0-1 --only easteregg   # 只发一个模块

为什么是「覆盖」而不是「替换」：APK 里始终带完整代码，这个包解压到客户端
私有目录后挂在 garmin_cn.__path__ 最前面，同名模块被盖掉，其余照旧。所以
下载失败、包损坏、离线首装，全都退回包里那份。

**只发增量是个陷阱**：`--only` 只用于「验证机制通不通」这种一次性实验。
正常发版请整包发（不加 --only）——半新半旧的模块组合会出「函数签名对不上」
这种最难查的错。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "garmin_cn"
REMOTE_DIR = "/opt/runnercoach/codepack"


def _gradle_version() -> str:
    f = ROOT / "android" / "app" / "build.gradle.kts"
    for line in f.read_text(encoding="utf-8").splitlines():
        if "versionName" in line:
            return line.split('"')[1]
    return "0.0.0"


def _ssh(a, cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", a.key, "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new", f"{a.user}@{a.host}", cmd],
        capture_output=True, text=True, errors="replace")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--key", default=str(Path.home() / ".ssh" / "runnercoach_ed25519"))
    ap.add_argument("--version", default="", help="包版本，如 1.5.0-1；默认取 gradle 版本 + 时间戳")
    ap.add_argument("--notes", default="")
    ap.add_argument("--only", default="", help="逗号分隔的模块名（不含 .py），只发这些")
    ap.add_argument("--probe", action="store_true",
                    help="额外塞一个 _packprobe.py：只有代码包真的接管导入才 import 得到，"
                         "用来验证机制（正式发版别开）")
    a = ap.parse_args(argv)

    ver = a.version or f"{_gradle_version()}-{int(time.time()) % 100000}"
    only = {x.strip() for x in a.only.split(",") if x.strip()}

    # codepack.py 永不进包：它是「装载器」，必须始终来自 APK。
    # 否则一个坏包会把装载逻辑自己换掉，下一次连回退的能力都没有。
    ALWAYS_LOCAL = {"codepack.py"}
    files = sorted(p for p in SRC.glob("*.py") if p.name not in ALWAYS_LOCAL)
    if only:
        files = [p for p in files if p.stem in only]
    if not files:
        print("× 没有要发布的模块")
        return 2
    if not only and (SRC / "appapi.py") not in files:
        print("× 整包里没有 appapi.py，客户端会拒收")
        return 2

    fname = f"code-{ver}.zip"
    out = ROOT / "dist" / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            z.write(p, f"garmin_cn/{p.name}")
        if a.probe:
            z.writestr("garmin_cn/_packprobe.py",
                       f'"""发布验证用的探针：这个文件只存在于代码包里。"""\n'
                       f'VERSION = "{ver}"\n')

    data = out.read_bytes()
    meta = {"version": ver, "file": fname, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "notes": a.notes, "published": time.strftime("%Y-%m-%d"),
            "modules": len(files), "partial": bool(only),
            # 这个包是给哪个 APK 版本打的。客户端**必须**拿它和当前
            # __version__ 比对：代码包是覆盖式的，装在旧 APK 上等于把
            # 新 APK 的修复盖回去（实测过——1.5.1-1 盖住 1.5.2，
            # apk_fetch 直接消失，应用内更新报「未知操作」）。
            "base_version": _gradle_version()}
    print(f"包    {fname}  {len(files)} 个模块  {len(data) / 1024:.0f} KB")
    print(f"版本  {ver}" + ("（部分模块）" if only else ""))
    print(f"SHA256 {meta['sha256'][:32]}…")

    remote_tmp = f"/tmp/{fname}"
    up = subprocess.run(
        ["scp", "-i", a.key, "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=accept-new",
         str(out), f"{a.user}@{a.host}:{remote_tmp}"],
        capture_output=True, text=True, errors="replace")
    if up.returncode != 0:
        print("× 上传失败：", (up.stderr or "").strip()[:200])
        return 1

    payload = json.dumps(meta, ensure_ascii=False)
    cmd = (f"sudo install -d -o runnercoach -g runnercoach {REMOTE_DIR} && "
           f"sudo mv -f {remote_tmp} {REMOTE_DIR}/{fname} && "
           f"sudo chmod 644 {REMOTE_DIR}/{fname} && "
           f"printf '%s' '{payload}' | sudo tee {REMOTE_DIR}/latest.json >/dev/null")
    r = _ssh(a, cmd)
    if r.returncode != 0:
        print("× 远端写失败：", (r.stderr or r.stdout or "").strip()[:300])
        return 1

    time.sleep(1)
    r = _ssh(a, f"sudo cat {REMOTE_DIR}/latest.json")
    out_txt = (r.stdout or "").strip()
    print("→ 远端确认：", out_txt[:80])
    if meta["sha256"] not in out_txt:
        print("× latest.json 与刚上传的不一致")
        return 1
    print("\n发布完成。客户端下次启动会取到：")
    print(f"  {ver} / {len(data) / 1024:.0f} KB")
    print("  已下载的包**下次启动**生效，运行中不热替换。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
