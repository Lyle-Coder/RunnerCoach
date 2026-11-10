"""确保国区会话有效：失效时用 config.json 里的账号静默重登。

用法：
    python tools/ensure_session.py            # 有效就什么都不做
    python tools/ensure_session.py 123456     # 遇到邮箱验证码时带上

退出码：0 有效 / 3 需要验证码 / 2 登录失败
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))

from garmin_cn.auth import AuthError, GarminCNAuth, MfaRequired  # noqa: E402
from garmin_cn.client import GarminCNClient  # noqa: E402
from garmin_cn.net import Net  # noqa: E402

SESSION = HOME / "data" / "session.json"


def client() -> GarminCNClient:
    return GarminCNClient(Net(SESSION))


def alive() -> bool:
    try:
        client().display_name
        return True
    except Exception:
        return False


def main(argv: list[str]) -> int:
    if alive():
        print("会话有效")
        return 0

    cfg = {}
    cfg_path = HOME / "config.json"
    if cfg_path.exists():
        cfg = json.load(io.open(cfg_path, encoding="utf-8"))
    if not cfg.get("email") or not cfg.get("password"):
        print("× config.json 里没有账号密码，请先跑 run.bat login")
        return 2

    # 关键：旧会话文件里的 CASTGC / JWT_WEB 会顶掉新票据——带着它换票，
    # 结果是「Cookie 拿到了但数据接口 401」。登录前必须先把它挪走。
    if SESSION.exists():
        stale = SESSION.with_name("session.stale.json")
        try:
            if stale.exists():
                stale.unlink()
            SESSION.rename(stale)
        except OSError:
            pass

    auth = GarminCNAuth(HOME)
    try:
        auth.login(cfg["email"], cfg["password"])
    except MfaRequired:
        if len(argv) > 1:
            auth.submit_mfa(argv[1])
        else:
            print("× 需要邮箱验证码：python tools/ensure_session.py <验证码>")
            return 3
    except AuthError as e:
        print(f"× 登录失败：{e}")
        return 2

    print("已重新登录" if alive() else "× 登录成功但会话校验失败")
    return 0 if alive() else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
