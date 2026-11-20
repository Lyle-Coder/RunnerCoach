#!/usr/bin/env bash
# 在服务器上装「每天清理发版中间产物」的 systemd timer。
# 用法：sudo bash install-cleanup.sh
set -euo pipefail

APP=/opt/runnercoach
SRC=/tmp/cleanup.py

[ -f "$SRC" ] || { echo "缺少 $SRC"; exit 1; }

install -o root -g root -m 755 "$SRC" "$APP/cleanup.py"

cat > /etc/systemd/system/runnercoach-cleanup.service <<'EOF'
[Unit]
Description=RunnerCoach 清理发版中间产物（APK / 代码包 / 备份 / journal）
After=runnercoach.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/runnercoach/cleanup.py
Nice=10
IOSchedulingClass=idle
EOF

cat > /etc/systemd/system/runnercoach-cleanup.timer <<'EOF'
[Unit]
Description=每天 04:10 清理一次（在 03:20 的备份之后）

[Timer]
OnCalendar=*-*-* 04:10:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable runnercoach-cleanup.timer >/dev/null 2>&1
systemctl start runnercoach-cleanup.timer

echo "== 立刻跑一次（dry-run 先看）"
sudo -u root /usr/bin/python3 "$APP/cleanup.py" --dry-run
echo
echo "== 定时器状态"
systemctl list-timers runnercoach-cleanup --all --no-pager | head -3
