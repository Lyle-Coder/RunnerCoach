# -*- coding: utf-8 -*-
"""把开发机上已有的 `server/.env` 里的密钥，搬进服务器的 `/etc/runnercoach/env`。

为什么需要它
------------
`server/.env` 是本地跑网关时用的，里面已经有 SMTP 授权码、上游 Key、
管理令牌。部署时不该让人再手抄一遍——抄错一位，表现是"验证码发不出去"
或"模型 401"，而错误信息都不会指向"你打错字了"。

这个脚本只做一件事：按**白名单**取值，合并进目标文件，保留目标文件里的
注释与顺序。不在白名单里的一律不搬（尤其是 `RC_SERVICE_CLOSED`——那是
"整个服务关停"的开关，跟着搬过去会把新服务器直接关掉）。

用法：
    python deploy/import_env.py --from server/.env --into /etc/runnercoach/env
    python deploy/import_env.py --from server/.env --dry-run     # 只报会搬哪些键

不打印任何值，只打印键名和长度。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 只搬这些。路径类（DB_PATH / PORT / TLS）由 install.sh 决定，不搬；
# 策略类（FREE_GRANT / PAY_PROVIDER）也不搬——那是部署时的决定，不是本机残留。
WHITELIST = (
    "ADMIN_TOKEN",
    "UPSTREAM_BASE", "UPSTREAM_API_KEY", "UPSTREAM_MODEL",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
    "SMS_PROVIDER", "SMS_ACCESS_KEY_ID", "SMS_ACCESS_KEY_SECRET",
    "SMS_SIGN_NAME", "SMS_TEMPLATE_CODE",
    "RC_SHARE_CODE",
)


def parse_env(path: Path) -> dict[str, str]:
    """读 .env。三条宽容，都是为了别把带注释的旧文件读成错误的密钥：

    1. `KEY = value` 两边的空格 → 去掉（有人手写会带空格）；
    2. 行内注释 `KEY=v  # 说明` → 去掉（旧文件里到处是）；
    3. 引号包裹的值 → 剥引号。
    """
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        # 行内注释：两个以上空格或制表符之后视为注释（单个空格不算，
        # 因为值本身可能含空格，比如 SMTP_FROM=Runner Coach <a@b.c>）
        for sep in ("\t#", "  #"):
            if sep in v:
                v = v.split(sep, 1)[0]
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def merge(target: Path, values: dict[str, str]) -> list[str]:
    """就地改 target：已有的键替换值，没有的追加到末尾。返回动过的键名。"""
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in values:
                out.append(f"{k}={values[k]}")
                seen.add(k)
                continue
        out.append(ln)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    target.write_text("\n".join(out) + "\n", encoding="utf-8")
    return sorted(values)


def main() -> int:
    ap = argparse.ArgumentParser(description="把本机 .env 的密钥搬进部署 env")
    ap.add_argument("--from", dest="src", required=True, help="源 .env（如 server/.env）")
    ap.add_argument("--into", dest="dst", help="目标 env（如 /etc/runnercoach/env）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写")
    a = ap.parse_args()

    src = Path(a.src)
    if not src.is_file():
        print(f"· 没有 {src}，跳过（用模板里的占位值）")
        return 0

    got = parse_env(src)
    picked = {k: got[k] for k in WHITELIST if got.get(k)}
    skipped = [k for k in ("RC_SERVICE_CLOSED", "FREE_GRANT", "PAY_PROVIDER")
               if got.get(k)]

    if not picked:
        print(f"· {src} 里没有可搬的键")
        return 0

    print("· 从 %s 取到 %d 项：" % (src, len(picked)))
    for k, v in picked.items():
        if any(t in k for t in ("PASS", "KEY", "TOKEN", "SECRET")):
            print(f"    {k:<20} {len(v)} 位")
        else:
            print(f"    {k:<20} {v}")
    if skipped:
        # 这三个不搬是有意的：服务关停开关、赠送额度、支付通道，
        # 都属于"新环境重新决定"的东西。
        print("· 以下键**故意不搬**（部署时另行决定）：" + "、".join(skipped))

    if a.dry_run or not a.dst:
        return 0

    dst = Path(a.dst)
    changed = merge(dst, picked)
    print(f"√ 已写入 {dst}（{len(changed)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
