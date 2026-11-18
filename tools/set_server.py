# -*- coding: utf-8 -*-
"""把 App 的出厂服务器地址（和证书）写进包里。

    python tools/set_server.py https://1.2.3.4:8443 --cert deploy/certs/cert.pem
    python tools/set_server.py --show          # 看现在连的是哪
    python tools/set_server.py --reset         # 恢复默认（当前的分享站）

改完必须重新打包 APK——`_buildcfg.py` 和 `content/server-ca.pem` 都是
烧进安装包的东西，`android/sync_python.bat` 会把它们同步过去。

为什么要有这个脚本而不是手改：换服务器要同时改「地址、分享码、证书」三处，
漏一处的表现是「能连上但一直 401」或「握手失败」，排查会绕很远。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CFG = ROOT / "garmin_cn" / "_buildcfg.py"
CA = ROOT / "garmin_cn" / "content" / "server-ca.pem"

TEMPLATE = '''# -*- coding: utf-8 -*-
"""出厂配置：App 第一次启动该连哪。

这个文件**由 `tools/set_server.py` 生成**，不要手改——换服务器时跑一次
那条命令，它会把地址、分享码和证书一起写进来，免得三处改漏一处。

    python tools/set_server.py https://1.2.3.4:8443 --cert cert.pem

为什么烧进包里而不是运行时问：首次启动时 App 还不知道该问谁。这就是
"先有鸡还是先有蛋"，只能内置一个引导入口。之后每次启动前会向
`/v1/client/config` 要一次最新值（换地址不必重新发版），失败就用这里的。

留空 = 让用户自己填（设置里有「自定义服务地址」）。内测不建议留空：
朋友第一步就是面对一个空白输入框，比连不上更劝退。
"""

PLATFORM_BASE = {base!r}
PLATFORM_SHARE_CODE = {code!r}
'''


def _san_of(cert: Path) -> str:
    exe = shutil.which("openssl")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "x509", "-in", str(cert), "-noout",
                              "-ext", "subjectAltName"],
                             capture_output=True, text=True, timeout=10)
        return (out.stdout or "").replace(" ", "").strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="写入 App 的出厂服务器地址")
    ap.add_argument("base", nargs="?", help="https://IP:端口")
    ap.add_argument("--cert", help="服务器证书（自签时必填），会裁成 App 的内置根")
    ap.add_argument("--code", default="", help="分享码（只在走了分享网关时需要）")
    ap.add_argument("--show", action="store_true", help="看当前配置")
    ap.add_argument("--reset", action="store_true", help="恢复默认")
    a = ap.parse_args()

    from garmin_cn import _buildcfg as cur

    if a.show:
        print(f"地址   {cur.PLATFORM_BASE or '（空：让用户自己填）'}")
        print(f"分享码 {cur.PLATFORM_SHARE_CODE or '（无）'}")
        print(f"证书   {CA if CA.is_file() else '（无内置证书，走公共 CA）'}")
        return 0

    if a.reset:
        base = "https://garmin-daily-dashboard.app.workbuddy.host"
        code = "ORT7NXCECR"
        cfg = TEMPLATE.format(base=base, code=code)
        CFG.write_text(cfg, encoding="utf-8")
        if CA.is_file():
            CA.unlink()
        print("已恢复默认，证书已移除。记得重新打包 APK。")
        return 0

    if not a.base:
        ap.error("要么给地址，要么用 --show / --reset")

    base = a.base.strip().rstrip("/")
    if not re.match(r"^https://[^\s/]+$", base):
        print(f"× 地址要写成 https://主机:端口（现在是 {base!r}）")
        print("  http 不行：令牌会明文经过网络。无域名时用自签证书 + https。")
        return 2

    host = base.split("://", 1)[1].split(":")[0]

    if a.cert:
        cert = Path(a.cert).expanduser()
        if not cert.is_file():
            print(f"× 找不到证书 {cert}")
            return 2
        # 证书里的名字必须和地址对得上，否则客户端一定握手失败。
        # 这里先替用户看出来，别等到手机上才发现。
        san = _san_of(cert)
        if san and host not in san:
            print(f"× 证书的 SAN 里没有 {host}：")
            print(f"  {san}")
            print("  重新用 deploy/gen-cert.sh <IP> 生成一张带 IP SAN 的。")
            return 2
        CA.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cert, CA)
        print(f"√ 证书 → {CA.relative_to(ROOT)}")
    elif not CA.is_file():
        print("！没给 --cert，且包内没有内置证书。")
        print("  如果服务器用的是自签证书，装完会报 CERTIFICATE_VERIFY_FAILED。")

    CFG.write_text(TEMPLATE.format(base=base, code=a.code), encoding="utf-8")
    print(f"√ 地址 → {base}")
    print(f"√ 分享码 → {a.code or '（无）'}")
    print()
    print("下一步：重新打包 APK（_buildcfg.py 和证书都是烧进包里的）")
    print("  cd android && build.bat assembleRelease")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
