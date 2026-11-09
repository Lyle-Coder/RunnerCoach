#!/usr/bin/env bash
# 把 Python 包同步进 App 的 source set。
#
# 为什么是「复制」而不是「引用上层目录」：Chaquopy 只认 src/main/python 下的模块，
# 用符号链接在 Windows 上会出问题，gradle 里跨目录 srcDir 又容易和增量构建打架。
# 复制一份、构建前跑一次，简单且不会有两套代码各说一套——源头永远是仓库根目录。
set -eu
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
DST="app/src/main/python"

rm -rf "$DST/garmin_cn"
mkdir -p "$DST"
cp -R "$ROOT/garmin_cn" "$DST/garmin_cn"
find "$DST" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# 自检只在桌面端跑（`run.bat selftest`），App 里没有任何入口调用它。
# 近 2000 行断言进 APK 只是白占体积和启动时的字节码加载，同步时剔掉。
rm -f "$DST/garmin_cn/selftest.py"

echo "已同步 $(find "$DST/garmin_cn" -name '*.py' | wc -l) 个 .py 到 $DST/garmin_cn"
