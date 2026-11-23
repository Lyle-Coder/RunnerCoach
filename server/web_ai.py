# -*- coding: utf-8 -*-
"""网页版调 AI 的子进程：一个进程只服务一个用户。

为什么还要一个子进程
--------------------
`garmin_cn.llm` 把配置路径、`chatstore` 把历史文件都做成模块级常量，都跟
`GARMIN_HOME` 绑死。网页版是多用户的，父进程里 import 一次就串号了。
所以和 `web_render.py` 一个套路：父进程带上用户的 HOME 起一个短命进程，
做完就退。

用法（只由 webapp.py 调用）：
    GARMIN_HOME=... python3 web_ai.py config.get '{}'
    GARMIN_HOME=... python3 web_ai.py chat '{"text":"这周该练什么"}'
    GARMIN_HOME=... python3 web_ai.py test '{}'

stdout 只有一行 JSON：成功是动作结果，失败是 `{"ok": false, "error": ...}`。
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except ValueError:
        payload = {}
    if not action:
        sys.stdout.write(json.dumps({"ok": False, "error": "缺少动作"}))
        return 2
    try:
        from garmin_cn import aiapi

        out = aiapi.dispatch(action, payload if isinstance(payload, dict) else {})
    except Exception as e:      # noqa: BLE001 - 任何异常都要变成可读的 JSON
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # default=str：结果里有 Path / datetime 也不能让 json.dumps 抛错，
    # 那会让页面看到「渲染失败」而不知道真实原因。
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
