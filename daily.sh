#!/usr/bin/env bash
# 每天一次：同步 → 看板 → 一图流（同 daily.bat）。
#
# 和 Windows 版的差别只有一处：没有无头浏览器时，`card` 会自动退化成
# HTML 版并返回 0，所以这里不用特判。
#
# 定时跑法（Termux）：
#   1) 装 Termux:API 后可以 `termux-job-scheduler`
#   2) 更省事：装 Termux:Widget，把 setup-termux.sh 生成的「每日同步」放到桌面
set -u
cd "$(dirname "$0")" || exit 1

[ -x ./run.sh ] || { echo "× 先执行：chmod +x run.sh daily.sh"; exit 1; }

# 安卓会杀后台，长同步时先申请唤醒锁（没装 termux-api 就跳过）
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock 2>/dev/null || true
  trap 'termux-wake-unlock 2>/dev/null || true' EXIT
fi

echo "── 同步 ──"
./run.sh sync --days 3 || echo "  ! 同步没成功，下面用库里已有的数据"

echo
echo "── 看板 ──"
./run.sh report || exit 2

echo
echo "── 一图流 ──"
./run.sh card || true

echo
echo "好了。手机上看：./run.sh serve   然后浏览器打开 http://127.0.0.1:8765"
