# -*- coding: utf-8 -*-
"""把一条反馈挂到 Gitee issue。

    python tools/gitee_issue.py --title "组页地图在 4G 下不显示" \
        --body "原话：……\\n复现：……" --type bug --label 待复现

    python tools/gitee_issue.py --title "..." --body @/tmp/body.md --dry-run

为什么单独写一个
----------------
这个仓库的 issue 是**项目型**，标准 REST 路径
`POST /repos/{owner}/{repo}/issues` 一律 404（返回 `project or enterprise`
或 `Not Found Project`，不是权限问题）。必须走**扁平路径** + 表单里带
`repo=` + `issue_type=`。这个坑踩过一次，写进代码比记在脑子里可靠。

issue 编号也是字母数字（如 `IKULZR`），不是自增数字，别按整数解析。

凭据
----
只从 `GITEE_TOKEN` 读；进程里没有时（Git Bash 不继承用户级变量），
Windows 下再从 HKCU\\Environment 取一次。不落盘、不打印、不进日志。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://gitee.com/api/v5"
OWNER = os.environ.get("GITEE_OWNER", "liyufei-lyle")
REPO = os.environ.get("GITEE_REPO", "RunnerCoach")
TYPES = ("bug", "需求", "任务")
MAX_TITLE = 80


class IssueError(Exception):
    """可预期、直接展示给人的错误。"""


def token() -> str:
    t = (os.environ.get("GITEE_TOKEN") or "").strip()
    if t:
        return t
    if os.name == "nt":
        # Git Bash 拿不到用户级变量，读注册表比调 PowerShell 干净
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v, _ = winreg.QueryValueEx(k, "GITEE_TOKEN")
                return str(v).strip()
        except OSError:
            pass
    raise IssueError(
        "没有 GITEE_TOKEN。PowerShell 里设一次（用户级，重启终端生效）：\n"
        '  [Environment]::SetEnvironmentVariable("GITEE_TOKEN","<令牌>","User")')


def _req(method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
    body = None
    url = BASE + path
    if data is not None:
        data = dict(data)
        data["access_token"] = token()
        body = urllib.parse.urlencode(data).encode("utf-8")
    else:
        url += ("?" if "?" not in url else "&") + urllib.parse.urlencode(
            {"access_token": token()})
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            raise IssueError(f"HTTP {e.code}：{raw[:200]}") from None
    except urllib.error.URLError as e:
        raise IssueError(f"连不上 gitee：{e.reason}") from None


def whoami() -> dict:
    st, d = _req("GET", "/user")
    if st != 200:
        raise IssueError(f"令牌无效（HTTP {st}）：{d.get('message') or d}")
    return d


def create(title: str, body: str, itype: str, labels: list[str],
           dry: bool = False) -> dict:
    title = title.strip()
    if not title:
        raise IssueError("标题不能为空")
    if len(title) > MAX_TITLE:
        raise IssueError(f"标题超过 {MAX_TITLE} 字：{len(title)}")
    if itype not in TYPES:
        raise IssueError("type 只能是 " + " / ".join(TYPES))

    # 项目型 issue 的入口：扁平路径 + repo + issue_type
    path = f"/repos/{OWNER}/issues"
    data = {"repo": REPO, "title": title, "body": body, "issue_type": itype}
    if labels:
        data["labels"] = ",".join(labels)
    if dry:
        return {"dry_run": True, "path": path, "data": data}
    st, d = _req("POST", path, data)
    if st not in (200, 201):
        raise IssueError(
            f"创建失败（HTTP {st}）：{d.get('message') or d}\n"
            "  404 通常不是权限问题，而是路径/参数不对：项目型 issue 必须用扁平路径")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="把一条反馈挂到 Gitee issue")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="", help="正文；@文件 可读文件")
    ap.add_argument("--type", default="bug", choices=TYPES)
    ap.add_argument("--label", default="", help="逗号分隔，标签要仓库里已存在")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    body = a.body
    if body.startswith("@"):
        with open(body[1:], encoding="utf-8") as f:
            body = f.read()
    labels = [x.strip() for x in a.label.split(",") if x.strip()]

    try:
        if a.dry_run:
            me = whoami()
            print(f"令牌有效：{me.get('login')}")
        d = create(a.title, body, a.type, labels, a.dry_run)
        if a.dry_run:
            print("== dry-run，没有提交 ==")
            print(json.dumps(d, ensure_ascii=False, indent=2)[:800])
            return 0
        print(f"已创建 issue {d.get('number')} · {d.get('html_url')}")
        return 0
    except IssueError as e:
        print(f"× {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
