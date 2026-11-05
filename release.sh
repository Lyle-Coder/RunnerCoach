#!/usr/bin/env bash
# RunnerCoach 发布助手（与 run.sh 同款解释器探测）。
#
#   ./release.sh status
#   ./release.sh check
#   ./release.sh prepare
#   ./release.sh tag --push
#   ./release.sh upload --tag v0.1.0 --dry-run
set -u
cd "$(dirname "$0")" || exit 1

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys' >/dev/null 2>&1; then
      PY="$c"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "× 没找到可用的 python3"
  exit 9009
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
exec "$PY" tools/release.py "$@"
