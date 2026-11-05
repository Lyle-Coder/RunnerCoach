#!/usr/bin/env bash
# run.bat 的 POSIX 版：Termux / Linux / macOS 通用。
#
# 用法和 run.bat 完全一样：
#   ./run.sh doctor
#   ./run.sh login
#   ./run.sh sync --days 30
#   ./run.sh report
#   ./run.sh serve
#
# 想强制指定解释器：PYTHON=/path/to/python3 ./run.sh doctor
set -u
cd "$(dirname "$0")" || exit 1

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    # 有些机器（比如 Windows 的 Store 版）python3 是 0 字节占位符：
    # 执行它没有任何输出、直接返回非 0。必须实测一次 import 才算数。
    if "$c" -c 'import sys' >/dev/null 2>&1; then
      PY="$c"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "× 没找到可用的 python3"
  echo "  Termux      : pkg install python"
  echo "  Debian/Ubuntu: sudo apt install python3"
  echo "  macOS       : brew install python"
  exit 9009
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "× Python 版本过低：$("$PY" -V 2>&1)，需要 3.11+"
  exit 9008
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

if [ "$#" -eq 0 ]; then
  exec "$PY" cli.py status
fi
exec "$PY" cli.py "$@"
