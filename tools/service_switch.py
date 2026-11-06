# -*- coding: utf-8 -*-
"""一键切换 RunnerCoach 平台服务的「关闭 / 恢复」。

    python tools/service_switch.py off      关闭：停网关容器 + 8300 顶维护态服务
    python tools/service_switch.py on       恢复：撤维护态 + 启动网关容器
    python tools/service_switch.py status   查看当前状态

关闭期间：公网入口与分享码不变，隧道客户端继续指向 8300，只是所有
认证 / 问答 / 支付接口立即回「服务已关闭」，看板顶部显示关闭横幅。
不会删除任何数据，容器与数据卷都保留。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
ENV_FILE = SERVER / ".env"
PID_FILE = SERVER / "data" / "maintenance.pid"
LOG_FILE = SERVER / "data" / "maintenance.log"
MAINT = ROOT / "tools" / "maintenance_server.py"
LOCAL = "http://127.0.0.1:8300"
# 本机探活必须直连：会话里可能注入 HTTP_PROXY，回环地址被代理转发会误判
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(args, cwd=ROOT):
    return subprocess.run(args, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def probe():
    """closed=维护态 / open=真实网关 / down=端口无人监听。"""
    try:
        with _LOCAL_OPENER.open(LOCAL + "/health", timeout=2) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        return j.get("service") or ("open" if j.get("ok") else "unknown"), j
    except Exception:
        return "down", {}


def set_env_closed(flag: bool):
    """.env 里的开关与运行状态保持一致，防止容器被误启动时又开放服务。"""
    want = "1" if flag else "0"
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    out, seen = [], False
    for line in lines:
        s = line.strip()
        if "=" in s and not s.startswith("#") and s.split("=", 1)[0].strip() == "RC_SERVICE_CLOSED":
            out.append(f"RC_SERVICE_CLOSED = {want}\n")
            seen = True
        else:
            out.append(line)
    if not seen:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("\n# 维护态开关：1=关闭邮箱认证/大模型/支付，0=正常开放\n")
        out.append(f"RC_SERVICE_CLOSED = {want}\n")
    ENV_FILE.write_text("".join(out), encoding="utf-8")


def kill_maintenance():
    if not PID_FILE.is_file():
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        pid = 0
    if pid:
        if sys.platform == "win32":
            run(["taskkill", "/F", "/PID", str(pid)])
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    PID_FILE.unlink(missing_ok=True)
    return bool(pid)


def start_maintenance():
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        # 尽量脱离调用方所属的 job：某些会话管理器会在命令结束时
        # 清理整棵子进程树，DETACHED_PROCESS 单独用不够。
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | 0x01000000)          # CREATE_BREAKAWAY_FROM_JOB
    env = dict(os.environ)
    env["PORT"] = "8300"
    with LOG_FILE.open("ab") as log:
        try:
            p = subprocess.Popen([sys.executable, str(MAINT)], cwd=str(ROOT),
                                 env=env, stdout=log, stderr=log,
                                 stdin=subprocess.DEVNULL,
                                 creationflags=flags, start_new_session=(flags == 0))
        except OSError:
            # 父 job 不允许 breakaway：退回普通分离模式
            flags &= ~0x01000000
            p = subprocess.Popen([sys.executable, str(MAINT)], cwd=str(ROOT),
                                 env=env, stdout=log, stderr=log,
                                 stdin=subprocess.DEVNULL,
                                 creationflags=flags, start_new_session=(flags == 0))
    return p.pid


def wait_state(want, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        state, _ = probe()
        if state == want:
            return True
        time.sleep(0.4)
    return False


def off():
    print("[1/4] 同步 .env 维护开关 -> 1")
    set_env_closed(True)
    print("[2/4] 停止网关容器（数据卷保留）")
    r = run(["docker", "compose", "stop"], SERVER)
    print("      " + (r.stdout.strip() or r.stderr.strip() or "已停止"))
    state, _ = probe()
    if state == "closed":
        print("[3/4] 8300 已是维护态，跳过启动")
        return status()
    print("[4/4] 在 8300 启动维护态占位服务")
    pid = start_maintenance()
    print(f"      maintenance pid={pid}  日志={LOG_FILE}")
    ok = wait_state("closed")
    print("      " + ("√ 公网入口现在会明确显示「服务已关闭」" if ok else
                     "✗ 维护态未就绪：若进程被会话清理，请直接运行 "
                     "python tools/maintenance_server.py（保持窗口/后台任务开着），"
                     "并检查 " + str(LOG_FILE)))
    return status()


def on():
    print("[1/4] 撤掉维护态占位服务")
    killed = kill_maintenance()
    print("      " + ("已停止占位服务" if killed else "没有在跑的占位服务"))
    print("[2/4] 同步 .env 维护开关 -> 0")
    set_env_closed(False)
    print("[3/4] 启动网关容器（带 --build，保证镜像里的维护开关与源码一致）")
    r = run(["docker", "compose", "up", "-d", "--build"], SERVER)
    print("      " + (r.stdout.strip() or r.stderr.strip() or "已启动")[-300:])
    ok = wait_state("open", timeout=40)
    print("[4/4] " + ("√ 服务已恢复" if ok else "✗ 网关未就绪，检查 docker compose logs"))
    return status()


def status():
    state, j = probe()
    r = run(["docker", "compose", "ps", "-a", "--format",
             "{{.Name}} {{.State}} {{.Status}}"], SERVER)
    containers = (r.stdout or r.stderr).strip()
    print("\n--- RunnerCoach 服务状态 ---")
    print("8300  : " + {"closed": "维护态（服务已关闭）", "open": "网关运行中（服务开放）",
                        "down": "无人监听"}.get(state, state)
          + (f"  email={j.get('email')} chat={j.get('chat')}" if j else ""))
    print("容器  : " + (containers or "（无）"))
    print("占位服务 pid: " + (PID_FILE.read_text(encoding="utf-8").strip()
                             if PID_FILE.is_file() else "（无）"))
    print("隧道客户端: 保持原样指向 8300，不需要重启")
    return 0 if state in ("closed", "open") else 1


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "off":
        return off()
    if cmd == "on":
        return on()
    if cmd == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
